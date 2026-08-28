"""Unit tests for the Docker sandbox adapter (no Docker daemon needed).

The docker client is injected through the adapter's lazy cache, so these
pin the container *configuration* — the security baseline and the CPU
maths — and the verdict mapping, fully offline.
"""

from __future__ import annotations

from typing import Any

from src.config import Settings
from src.core.domain import SandboxVerdict
from src.infrastructure.docker_sandbox import DockerSandbox


class _FakeContainer:
    def __init__(self, exit_code: int, logs: bytes) -> None:
        self._exit_code, self._logs = exit_code, logs

    def wait(self, timeout: float) -> dict[str, int]:
        return {"StatusCode": self._exit_code}

    def logs(self, **_: Any) -> bytes:
        return self._logs

    def remove(self, force: bool) -> None:
        return None


class _FakeContainers:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container
        self.run_kwargs: dict[str, Any] = {}

    def run(self, **kwargs: Any) -> _FakeContainer:
        self.run_kwargs = kwargs
        return self._container


class _FakeClient:
    def __init__(self, container: _FakeContainer) -> None:
        self.containers = _FakeContainers(container)


def _sandbox(
    exit_code: int = 0, logs: bytes = b"ok", **settings_kw: object
) -> tuple[DockerSandbox, _FakeClient]:
    settings = Settings(log_json=False, **settings_kw)
    sandbox = DockerSandbox(settings)
    client = _FakeClient(_FakeContainer(exit_code, logs))
    sandbox._client = client  # inject through the lazy cache
    return sandbox, client


def test_cpu_quota_converts_to_half_a_cpu_not_a_twentieth() -> None:
    """Regression for the dormant factor-10 bug: 50_000 µs/100ms = 0.5 CPU,
    which is 500_000_000 nano-CPUs — the old *1000 yielded 0.05 CPU."""
    sandbox, client = _sandbox()
    sandbox._run_blocking("/ws", "img", ("pytest",))
    assert client.containers.run_kwargs["nano_cpus"] == 500_000_000


def test_security_baseline_is_applied_unconditionally() -> None:
    sandbox, client = _sandbox()
    sandbox._run_blocking("/ws", "img", ("pytest",))
    kwargs = client.containers.run_kwargs
    assert kwargs["network_disabled"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["volumes"] == {"/ws": {"bind": "/workspace", "mode": "rw"}}


def test_sandbox_workdir_drives_mount_and_working_dir() -> None:
    """SWE-bench mounts at /testbed (the image's editable-install path)."""
    sandbox, client = _sandbox(sandbox_workdir="/testbed")
    sandbox._run_blocking("/ws", "img", ("pytest",))
    kwargs = client.containers.run_kwargs
    assert kwargs["working_dir"] == "/testbed"
    assert kwargs["volumes"] == {"/ws": {"bind": "/testbed", "mode": "rw"}}


def test_exit_code_maps_to_verdict() -> None:
    sandbox, _ = _sandbox(exit_code=0)
    verdict, exit_code, _logs = sandbox._run_blocking("/ws", "img", ("pytest",))
    assert verdict is SandboxVerdict.PASSED and exit_code == 0

    sandbox, _ = _sandbox(exit_code=1, logs=b"1 failed")
    verdict, exit_code, logs = sandbox._run_blocking("/ws", "img", ("pytest",))
    assert verdict is SandboxVerdict.FAILED and exit_code == 1
    assert "1 failed" in logs
