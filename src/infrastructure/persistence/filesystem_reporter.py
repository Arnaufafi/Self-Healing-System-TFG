"""Filesystem reporter writing Markdown post-mortems.

The blocking ``open(...).write()`` call is wrapped via
:func:`asyncio.to_thread` so the orchestrator coroutine remains free to
schedule other work (e.g. notifying observability backends in parallel
when those adapters are added).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from src.config import Settings
from src.core.domain import FailedAttempt, TriggerEvent
from src.core.ports import ReporterPort

_LOGGER = logging.getLogger(__name__)


class FilesystemReporter(ReporterPort):
    """Writes ``/reports/bug_report_<id>.md`` files."""

    def __init__(self, settings: Settings) -> None:
        """Store settings and ensure the output directory exists."""
        self._settings = settings
        self._settings.reports_dir.mkdir(parents=True, exist_ok=True)

    async def write_post_mortem(
        self,
        incident_id: str,
        trigger: TriggerEvent,
        attempts: tuple[FailedAttempt, ...],
        narrative: str = "",
    ) -> str:
        """See :meth:`ReporterPort.write_post_mortem`."""
        sanitised_id = _sanitise_filename(incident_id)
        path = self._settings.reports_dir / f"bug_report_{sanitised_id}.md"
        content = _render_markdown(incident_id, trigger, attempts, narrative)
        _LOGGER.info(
            "reporter.write_post_mortem.start",
            extra={"path": str(path), "attempts": len(attempts)},
        )
        await asyncio.to_thread(path.write_text, content, "utf-8")
        _LOGGER.info("reporter.write_post_mortem.done", extra={"path": str(path)})
        return str(path)


# ---------------------------------------------------------------------------
# Helpers (pure functions, easy to unit-test).
# ---------------------------------------------------------------------------


def _sanitise_filename(raw: str) -> str:
    """Reduce ``raw`` to a filesystem-safe filename fragment."""
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)[:120]


def _render_markdown(
    incident_id: str,
    trigger: TriggerEvent,
    attempts: Iterable[FailedAttempt],
    narrative: str = "",
) -> str:
    """Compose the Markdown body of a post-mortem report."""
    lines: list[str] = []
    lines.append(f"# Post-Mortem: incident `{incident_id}`")
    lines.append("")
    if narrative.strip():
        lines.append("## Summary")
        lines.append("")
        lines.append(narrative.strip())
        lines.append("")
    lines.append(f"- **Trigger type:** `{trigger.trigger_type.value}`")
    if trigger.crash_report is not None:
        lines.append(f"- **Service:** `{trigger.crash_report.service_name}`")
        lines.append(f"- **Commit SHA:** `{trigger.crash_report.commit_sha}`")
        lines.append(f"- **Captured at:** `{trigger.crash_report.captured_at.isoformat()}`")
    if trigger.failing_test is not None:
        lines.append(f"- **Failing test:** `{trigger.failing_test.node_id}`")
    lines.append("")
    lines.append("## Attempts")
    lines.append("")
    for attempt in attempts:
        lines.append(f"### Attempt #{attempt.attempt_index}")
        lines.append(f"- **Recorded at:** `{attempt.recorded_at.isoformat()}`")
        lines.append(f"- **Error summary:** {attempt.error_summary}")
        if attempt.sandbox_result is not None:
            lines.append(f"- **Sandbox verdict:** `{attempt.sandbox_result.verdict.value}`")
            lines.append(f"- **Sandbox exit code:** `{attempt.sandbox_result.exit_code}`")
            if attempt.sandbox_result.logs_tail.strip():
                lines.append("")
                lines.append("<details><summary>Sandbox output (tail)</summary>")
                lines.append("")
                lines.append("```")
                # rstrip("\n") only — preserve any trailing whitespace
                # inside lines (pytest summary tables rely on alignment).
                lines.append(attempt.sandbox_result.logs_tail.rstrip("\n"))
                lines.append("```")
                lines.append("")
                lines.append("</details>")
        if attempt.patch is not None:
            lines.append("")
            lines.append("<details><summary>Attempted patch</summary>")
            lines.append("")
            lines.append("```diff")
            # Use rstrip("\n") (not bare rstrip()) so that trailing
            # blank context lines (rendered as " \n" in unified diffs)
            # survive into the report.  A bare rstrip() eats the leading
            # space too, making valid 7-line hunks look like corrupt
            # 6-line ones and obscuring real bugs.
            lines.append(attempt.patch.diff_text.rstrip("\n"))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
        lines.append("")
    return "\n".join(lines) + "\n"
