"""Unit tests for the Pydantic domain models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.domain import (
    CrashReport,
    FailedAttempt,
    FailingTest,
    Patch,
    SandboxResult,
    SandboxVerdict,
    TriggerEvent,
    TriggerType,
)


class TestTriggerEvent:
    """Discriminated-union validation invariants."""

    def test_crash_trigger_requires_crash_report(self) -> None:
        """PRODUCTION_CRASH without payload must fail validation."""
        with pytest.raises(ValueError):
            TriggerEvent(trigger_type=TriggerType.PRODUCTION_CRASH)

    def test_test_failure_trigger_requires_failing_test(self) -> None:
        """TEST_FAILURE without payload must fail validation."""
        with pytest.raises(ValueError):
            TriggerEvent(trigger_type=TriggerType.TEST_FAILURE)

    def test_valid_crash_trigger(self) -> None:
        """A well-formed crash trigger is accepted."""
        crash = CrashReport(
            incident_id="i1",
            service_name="svc",
            stack_trace="trace",
            commit_sha="sha",
        )
        ev = TriggerEvent(trigger_type=TriggerType.PRODUCTION_CRASH, crash_report=crash)
        assert ev.crash_report is crash

    def test_valid_test_failure_trigger(self) -> None:
        """A well-formed test-failure trigger is accepted."""
        ft = FailingTest(node_id="n", source="src", last_failure_output="out")
        ev = TriggerEvent(trigger_type=TriggerType.TEST_FAILURE, failing_test=ft)
        assert ev.failing_test is ft


class TestFrozenness:
    """Every value object should be frozen."""

    def test_crash_report_is_frozen(self) -> None:
        """Mutating a CrashReport must raise."""
        c = CrashReport(incident_id="i", service_name="s", stack_trace="t", commit_sha="x")
        with pytest.raises(ValidationError):
            c.service_name = "other"  # type: ignore[misc]

    def test_patch_is_frozen(self) -> None:
        """Mutating a Patch must raise."""
        p = Patch(diff_text="--- a\n+++ b\n", author_agent="x")
        with pytest.raises(ValidationError):
            p.author_agent = "other"  # type: ignore[misc]


class TestFailedAttempt:
    """Sanity checks on the history record model."""

    def test_minimal_construction(self) -> None:
        """Only ``attempt_index`` and ``error_summary`` are mandatory."""
        rec = FailedAttempt(attempt_index=0, error_summary="boom")
        assert rec.patch is None
        assert rec.sandbox_result is None

    def test_negative_index_rejected(self) -> None:
        """Indices must be non-negative."""
        with pytest.raises(ValidationError):
            FailedAttempt(attempt_index=-1, error_summary="x")


class TestSandboxResult:
    """Sandbox result invariants."""

    def test_negative_duration_rejected(self) -> None:
        """Durations must be non-negative."""
        with pytest.raises(ValidationError):
            SandboxResult(verdict=SandboxVerdict.PASSED, duration_seconds=-1.0)
