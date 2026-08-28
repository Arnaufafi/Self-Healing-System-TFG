"""In-memory sandbox stub.

Used by unit tests and the TFG demo when Docker is unavailable. The
verdict is configured at construction time so deterministic scenarios
can be played out (e.g. "first run fails, second run passes").
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable

from src.core.domain import SandboxResult, SandboxVerdict
from src.core.ports import SandboxPort

_LOGGER = logging.getLogger(__name__)


class InMemorySandbox(SandboxPort):
    """Deterministic sandbox replacement."""

    def __init__(
        self,
        scripted_verdicts: Iterable[SandboxVerdict] = (),
        default_verdict: SandboxVerdict = SandboxVerdict.FAILED,
        per_call_latency_s: float = 0.0,
        scripted_results: Iterable[SandboxResult] = (),
    ) -> None:
        """Configure the scripted behaviour.

        Args:
            scripted_verdicts: Verdicts to emit in order; once exhausted
                ``default_verdict`` is returned indefinitely.
            default_verdict: Fallback verdict.
            per_call_latency_s: Synthetic delay to mimic real container
                start-up time.
            scripted_results: Full :class:`SandboxResult` objects to emit in
                order, taking precedence over ``scripted_verdicts``. Use this
                when a test needs to control ``logs_tail`` so the orchestrator
                fingerprints a specific (chained) error.
        """
        self._results: deque[SandboxResult] = deque(scripted_results)
        self._queue: deque[SandboxVerdict] = deque(scripted_verdicts)
        self._default = default_verdict
        self._latency = per_call_latency_s

    async def run_tests(
        self,
        workspace_path: str,
        image: str,
        command: tuple[str, ...],
    ) -> SandboxResult:
        """See :meth:`SandboxPort.run_tests`."""
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._results:
            result = self._results.popleft()
            _LOGGER.info(
                "sandbox.inmemory.run",
                extra={"verdict": result.verdict.value, "workspace": workspace_path},
            )
            return result
        verdict = self._queue.popleft() if self._queue else self._default
        _LOGGER.info(
            "sandbox.inmemory.run",
            extra={"verdict": verdict.value, "workspace": workspace_path},
        )
        return SandboxResult(
            verdict=verdict,
            exit_code=0 if verdict is SandboxVerdict.PASSED else 1,
            duration_seconds=self._latency,
            logs_tail=f"[in-memory sandbox] verdict={verdict.value}",
        )
