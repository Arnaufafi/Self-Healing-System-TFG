"""Unit tests for the conditional-edge routing functions (fix-first)."""

from __future__ import annotations

import pytest

from src.core.domain import (
    ErrorSignature,
    HealingState,
    Patch,
    RoutingDecision,
    SandboxResult,
    SandboxVerdict,
)
from src.orchestrator.routers import (
    route_after_commit,
    route_after_fix,
    route_after_rollback,
    route_after_validate,
)

_SIG_A = ErrorSignature(kind="crash", exc_type="TypeError", location="main.py:run")
_SIG_B = ErrorSignature(kind="crash", exc_type="ValueError", location="svc.py:save")


def _result(verdict: SandboxVerdict) -> SandboxResult:
    code = 0 if verdict is SandboxVerdict.PASSED else 1
    return SandboxResult(verdict=verdict, exit_code=code, duration_seconds=1.0)


# --- route_after_fix ---------------------------------------------------------


def test_fix_with_patch_goes_to_validation() -> None:
    state: HealingState = {"current_patch": Patch(diff_text="--- a\n+++ b\n", author_agent="mock")}
    assert route_after_fix(state) is RoutingDecision.TO_VALIDATION


def test_fix_without_patch_goes_to_rollback() -> None:
    assert route_after_fix({"current_patch": None}) is RoutingDecision.TO_ROLLBACK
    assert route_after_fix({}) is RoutingDecision.TO_ROLLBACK


# --- route_after_validate ----------------------------------------------------


def test_validate_green_goes_to_immunize() -> None:
    state: HealingState = {
        "current_sandbox_result": _result(SandboxVerdict.PASSED),
        "current_error_signature": _SIG_A,
    }
    assert route_after_validate(state) is RoutingDecision.TO_IMMUNIZE


def test_validate_changed_error_is_progress_to_immunize() -> None:
    """A different post-fix signature means the original error is gone."""
    state: HealingState = {
        "current_sandbox_result": _result(SandboxVerdict.FAILED),
        "current_error_signature": _SIG_A,
        "post_fix_signature": _SIG_B,
    }
    assert route_after_validate(state) is RoutingDecision.TO_IMMUNIZE


def test_validate_same_error_goes_to_rollback() -> None:
    state: HealingState = {
        "current_sandbox_result": _result(SandboxVerdict.FAILED),
        "current_error_signature": _SIG_A,
        "post_fix_signature": _SIG_A,
    }
    assert route_after_validate(state) is RoutingDecision.TO_ROLLBACK


def test_validate_unparseable_failure_goes_to_rollback() -> None:
    """Non-PASSED with no usable signature ⇒ cannot confirm progress ⇒ retry."""
    state: HealingState = {
        "current_sandbox_result": _result(SandboxVerdict.FAILED),
        "current_error_signature": _SIG_A,
        "post_fix_signature": None,
    }
    assert route_after_validate(state) is RoutingDecision.TO_ROLLBACK


def test_validate_regression_goes_to_rollback() -> None:
    """A regression (fix broke a protected test) must retry, never immunize —
    even though the reproduction itself went green."""
    state: HealingState = {
        "current_sandbox_result": _result(SandboxVerdict.FAILED),
        "current_error_signature": _SIG_A,
        "regression_detected": True,
    }
    assert route_after_validate(state) is RoutingDecision.TO_ROLLBACK


# --- route_after_commit ------------------------------------------------------


def test_commit_continues_chain_when_flagged() -> None:
    assert route_after_commit({"should_continue": True}) is RoutingDecision.TO_FIX


def test_commit_finishes_when_not_flagged() -> None:
    assert route_after_commit({"should_continue": False}) is RoutingDecision.FINISH
    assert route_after_commit({}) is RoutingDecision.FINISH


# --- route_after_rollback ----------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "limit", "expected"),
    [
        (0, 3, RoutingDecision.TO_FIX),
        (2, 3, RoutingDecision.TO_FIX),
        (3, 3, RoutingDecision.TO_POST_MORTEM),
        (4, 3, RoutingDecision.TO_POST_MORTEM),
    ],
)
def test_rollback_retry_budget(attempts: int, limit: int, expected: RoutingDecision) -> None:
    state: HealingState = {"attempt_count": attempts, "max_retries": limit}
    assert route_after_rollback(state) is expected
