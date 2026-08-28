"""Reporter-agent port.

The **Reporter** is the only narrative agent in the pipeline.  It does not
touch the workspace or the sandbox: given structured facts about a healed
error (or an unrecoverable one) it composes human-readable prose — a
Conventional-Commits message for each fix, and the post-mortem narrative
when the retry budget is exhausted.

Kept separate from :class:`~src.core.ports.reporter.ReporterPort` (which
*persists* a post-mortem to a sink): this port *writes the text*, that port
*stores it*.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.domain import FailedAttempt, ResolvedError, TriggerEvent


@runtime_checkable
class ReporterAgentPort(Protocol):
    """Composes commit messages and post-mortem narratives."""

    async def compose_commit_message(
        self,
        *,
        incident_id: str,
        error_signature: str,
        diff_text: str,
        prior_attempts: tuple[str, ...],
        test_path: str | None,
    ) -> str:
        """Write a Conventional-Commits message for one healed error.

        Args:
            incident_id: Stable identifier of the incident.
            error_signature: Human-readable fingerprint of the error fixed.
            diff_text: Unified diff of the fix (for the agent to summarise).
            prior_attempts: Tails of earlier failed attempts on this error.
            test_path: Regression test file written, if any.

        Returns:
            A commit message (subject + body). Implementations should be
            best-effort and never raise for content reasons — fall back to
            a deterministic template instead.
        """
        ...

    async def compose_post_mortem(
        self,
        *,
        incident_id: str,
        trigger: TriggerEvent,
        attempts: tuple[FailedAttempt, ...],
        resolved: tuple[ResolvedError, ...],
    ) -> str:
        """Write a narrative post-mortem summary (Markdown).

        Returns:
            A short narrative prepended to the structured report by the
            persistence layer. Best-effort; may return ``""`` on failure.
        """
        ...
