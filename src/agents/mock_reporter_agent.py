"""In-memory Reporter used for development and the TFG defence demo.

Conforms to :class:`src.core.ports.ReporterAgentPort`.  Emits deterministic
Conventional-Commits messages and a short post-mortem narrative without any
LLM call, so the full pipeline runs offline.
"""

from __future__ import annotations

import asyncio
import logging

from src.core.domain import FailedAttempt, ResolvedError, TriggerEvent

_LOGGER = logging.getLogger(__name__)


class MockReporterAgent:
    """Deterministic offline Reporter (commit messages + post-mortem)."""

    def __init__(self, *, artificial_latency_s: float = 0.0) -> None:
        """Initialise the mock with an optional synthetic latency."""
        if artificial_latency_s < 0:
            raise ValueError("artificial_latency_s must be non-negative")
        self._latency = artificial_latency_s

    async def compose_commit_message(
        self,
        *,
        incident_id: str,
        error_signature: str,
        diff_text: str,
        prior_attempts: tuple[str, ...],
        test_path: str | None,
    ) -> str:
        """See :meth:`ReporterAgentPort.compose_commit_message`."""
        if self._latency:
            await asyncio.sleep(self._latency)
        subject = f"fix(self-healing): resolve {error_signature}"
        body = (
            f"Auto-healed incident {incident_id}.\n"
            f"Attempts before success: {len(prior_attempts)}.\n"
            f"Diff size: {len(diff_text)} bytes.\n"
        )
        if test_path:
            body += f"Regression test appended to {test_path}.\n"
        _LOGGER.info("reporter.commit_message.done", extra={"incident_id": incident_id})
        return f"{subject}\n\n{body}"

    async def compose_post_mortem(
        self,
        *,
        incident_id: str,
        trigger: TriggerEvent,
        attempts: tuple[FailedAttempt, ...],
        resolved: tuple[ResolvedError, ...],
    ) -> str:
        """See :meth:`ReporterAgentPort.compose_post_mortem`."""
        if self._latency:
            await asyncio.sleep(self._latency)
        _LOGGER.info("reporter.post_mortem.done", extra={"incident_id": incident_id})
        return (
            f"The self-healing pipeline exhausted its retry budget on incident "
            f"`{incident_id}` after {len(attempts)} failed attempt(s); "
            f"{len(resolved)} earlier error(s) in the chain were healed."
        )
