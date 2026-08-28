"""Reporting port.

Decouples the post-mortem node from any concrete storage backend
(filesystem today, S3 / GCS / Jira in production).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.domain import FailedAttempt, TriggerEvent


@runtime_checkable
class ReporterPort(Protocol):
    """Persists a post-mortem describing an unrecoverable cycle."""

    async def write_post_mortem(
        self,
        incident_id: str,
        trigger: TriggerEvent,
        attempts: tuple[FailedAttempt, ...],
        narrative: str = "",
    ) -> str:
        """Write the post-mortem and return the destination URI.

        Args:
            incident_id: Stable identifier shared with the trigger.
            trigger: Original event that opened the cycle.
            attempts: Full history of failed attempts, oldest first.
            narrative: Optional Reporter-authored prose prepended to the
                structured report. Empty when no narrative was produced.

        Returns:
            The location where the report was written (e.g. an absolute
            filesystem path, an ``s3://`` URI, etc.).
        """
        ...
