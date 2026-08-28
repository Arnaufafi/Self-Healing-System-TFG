"""Unit tests for :mod:`src.infrastructure.persistence.filesystem_reporter`.

Covers the rendering invariants that matter to a human reading a
post-mortem — particularly that the sandbox ``logs_tail`` (the actual
pytest output that caused the rollback) is surfaced, since without it
the report is unactionable.
"""

from __future__ import annotations

from src.core.domain import (
    CrashReport,
    FailedAttempt,
    Patch,
    SandboxResult,
    SandboxVerdict,
    TriggerEvent,
    TriggerType,
)
from src.infrastructure.persistence.filesystem_reporter import _render_markdown


def _trigger() -> TriggerEvent:
    return TriggerEvent(
        trigger_type=TriggerType.PRODUCTION_CRASH,
        crash_report=CrashReport(
            incident_id="inc-rep",
            service_name="svc",
            stack_trace="trace",
            commit_sha="abc",
        ),
    )


def test_logs_tail_is_rendered_when_present() -> None:
    """A non-empty ``logs_tail`` must appear in a collapsible block."""
    attempt = FailedAttempt(
        attempt_index=0,
        patch=Patch(diff_text="--- a\n+++ b\n", author_agent="mock"),
        sandbox_result=SandboxResult(
            verdict=SandboxVerdict.FAILED,
            exit_code=1,
            duration_seconds=0.1,
            logs_tail="E   AssertionError: expected 42, got 7\n",
        ),
        error_summary="sandbox verdict=failed",
    )
    md = _render_markdown("inc-rep", _trigger(), (attempt,))
    assert "<details><summary>Sandbox output (tail)</summary>" in md
    assert "AssertionError: expected 42, got 7" in md
    # And it must be inside a fenced code block, otherwise pytest output
    # with leading whitespace would render as broken Markdown.
    fenced = md.split("<details><summary>Sandbox output (tail)</summary>", 1)[1]
    assert "```" in fenced.split("</details>", 1)[0]


def test_logs_tail_block_omitted_when_blank() -> None:
    """Empty ``logs_tail`` must not produce an empty <details> block."""
    attempt = FailedAttempt(
        attempt_index=0,
        sandbox_result=SandboxResult(
            verdict=SandboxVerdict.FAILED,
            exit_code=1,
            duration_seconds=0.1,
            logs_tail="",
        ),
        error_summary="sandbox verdict=failed",
    )
    md = _render_markdown("inc-rep", _trigger(), (attempt,))
    assert "Sandbox output (tail)" not in md


def test_logs_tail_block_omitted_when_whitespace_only() -> None:
    """Pure-whitespace tails are noise; do not render them either."""
    attempt = FailedAttempt(
        attempt_index=0,
        sandbox_result=SandboxResult(
            verdict=SandboxVerdict.FAILED,
            exit_code=1,
            duration_seconds=0.1,
            logs_tail="   \n\n",
        ),
        error_summary="sandbox verdict=failed",
    )
    md = _render_markdown("inc-rep", _trigger(), (attempt,))
    assert "Sandbox output (tail)" not in md
