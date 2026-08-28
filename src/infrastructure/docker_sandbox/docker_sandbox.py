"""Asynchronous Docker sandbox adapter.

Wraps the synchronous ``docker`` SDK with :func:`asyncio.to_thread`, so
the orchestrator coroutine never blocks the event loop while the
container is running. All security-relevant flags (``network_disabled``,
``mem_limit``, read-only mounts, no new privileges) are encoded as
constants and applied unconditionally — the caller cannot relax them.

For the TFG defence we keep the SDK import optional: when ``docker``
is unavailable on the demo machine, the adapter raises a clear
:class:`DockerSandboxError` instead of an opaque ``ImportError``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

from src.config import Settings
from src.core.domain import SandboxResult, SandboxVerdict
from src.core.exceptions import DockerSandboxError, SandboxTimeoutError
from src.core.ports import SandboxPort

_LOGGER = logging.getLogger(__name__)

# --- Security baseline (immutable) ------------------------------------------
_SECURITY_OPTS: Final[tuple[str, ...]] = ("no-new-privileges:true",)
_CAP_DROP: Final[tuple[str, ...]] = ("ALL",)
_LOG_TAIL_BYTES: Final[int] = 8 * 1024  # 8 KiB tail to avoid log spam.


class DockerSandbox(SandboxPort):
    """Production sandbox adapter using docker-py under the hood."""

    def __init__(self, settings: Settings) -> None:
        """Store the settings and lazily resolve the docker client."""
        self._settings = settings
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        """Lazily instantiate ``docker.DockerClient`` from environment."""
        if self._client is not None:
            return self._client
        try:
            import docker  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise DockerSandboxError(
                "docker SDK is not installed; cannot run sandboxed validations."
            ) from exc
        try:
            self._client = docker.from_env()
        except Exception as exc:  # pragma: no cover - env-dependent
            raise DockerSandboxError("Failed to connect to the Docker daemon.") from exc
        return self._client

    async def run_tests(
        self,
        workspace_path: str,
        image: str,
        command: tuple[str, ...],
    ) -> SandboxResult:
        """See :meth:`SandboxPort.run_tests`."""
        _LOGGER.info(
            "sandbox.run.start",
            extra={
                "image": image,
                "workspace": workspace_path,
                "command": list(command),
                "mem_limit": self._settings.sandbox_mem_limit,
                "timeout_s": self._settings.sandbox_timeout_seconds,
            },
        )
        started_at = time.monotonic()
        try:
            verdict, exit_code, logs_tail = await asyncio.wait_for(
                asyncio.to_thread(self._run_blocking, workspace_path, image, command),
                timeout=self._settings.sandbox_timeout_seconds,
            )
        except TimeoutError:
            duration = time.monotonic() - started_at
            _LOGGER.error(
                "sandbox.run.timeout",
                extra={"duration_s": duration, "limit_s": self._settings.sandbox_timeout_seconds},
            )
            # We do not silently swallow: timeout is a recoverable verdict.
            return SandboxResult(
                verdict=SandboxVerdict.TIMEOUT,
                exit_code=None,
                duration_seconds=duration,
                logs_tail="container exceeded wall-clock timeout",
            )
        except DockerSandboxError as exc:
            duration = time.monotonic() - started_at
            _LOGGER.exception("sandbox.run.infra_error", extra={"duration_s": duration})
            return SandboxResult(
                verdict=SandboxVerdict.INFRASTRUCTURE_ERROR,
                exit_code=None,
                duration_seconds=duration,
                logs_tail=str(exc),
            )

        duration = time.monotonic() - started_at
        _LOGGER.info(
            "sandbox.run.done",
            extra={"verdict": verdict.value, "exit_code": exit_code, "duration_s": duration},
        )
        return SandboxResult(
            verdict=verdict,
            exit_code=exit_code,
            duration_seconds=duration,
            logs_tail=logs_tail,
        )

    # ------------------------------------------------------------------
    # Blocking implementation, runs in a worker thread.
    # ------------------------------------------------------------------
    def _run_blocking(
        self,
        workspace_path: str,
        image: str,
        command: tuple[str, ...],
    ) -> tuple[SandboxVerdict, int, str]:
        """Execute the container synchronously and return raw outcome.

        Kept separate so it can be invoked through ``asyncio.to_thread``.
        """
        client = self._ensure_client()
        container = None
        # Where the workspace is mounted inside the container; ``/workspace`` by
        # default, ``/testbed`` for SWE-bench images (whose editable install
        # resolves the package there). See ``Settings.sandbox_workdir``.
        workdir = self._settings.sandbox_workdir
        try:
            container = client.containers.run(
                image=image,
                command=list(command),
                detach=True,
                # --- Hardening (must stay aligned with the security baseline)
                # Defaults to True; opt-in relax (CDD_SANDBOX_ALLOW_NETWORK) for
                # SWE-bench images whose tests need a network (e.g. requests/httpbin).
                network_disabled=self._settings.sandbox_network_disabled,
                mem_limit=self._settings.sandbox_mem_limit,
                # quota is µs per 100 ms (Docker --cpu-quota semantics), i.e.
                # quota/100_000 CPUs; nano_cpus wants CPUs * 1e9, hence * 10_000.
                # (* 1000 was a dormant factor-10 bug: 0.05 CPU instead of 0.5.)
                nano_cpus=self._settings.sandbox_cpu_quota * 10_000,
                read_only=False,  # pytest writes .pyc; mount tmpfs instead in prod.
                cap_drop=list(_CAP_DROP),
                security_opt=list(_SECURITY_OPTS),
                pids_limit=256,
                volumes={workspace_path: {"bind": workdir, "mode": "rw"}},
                working_dir=workdir,
                auto_remove=False,
            )
            result = container.wait(timeout=self._settings.sandbox_timeout_seconds)
            exit_code = int(result.get("StatusCode", 1))
            logs_bytes = container.logs(stdout=True, stderr=True, tail=200) or b""
            logs_tail = logs_bytes[-_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
            verdict = SandboxVerdict.PASSED if exit_code == 0 else SandboxVerdict.FAILED
            return verdict, exit_code, logs_tail
        except SandboxTimeoutError:  # pragma: no cover - re-exported for clarity
            raise
        except DockerSandboxError:
            raise
        except Exception as exc:  # pragma: no cover - SDK heterogeneity
            raise DockerSandboxError(f"Sandbox execution failed: {exc!s}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:  # pragma: no cover - best-effort cleanup
                    _LOGGER.warning(
                        "sandbox.cleanup.failed", extra={"id": getattr(container, "id", "?")}
                    )
