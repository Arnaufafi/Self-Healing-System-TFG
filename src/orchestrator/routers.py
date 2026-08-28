"""Conditional edge functions for the LangGraph orchestrator.

Each function is pure: it inspects a snapshot of :class:`HealingState`
and returns a :class:`RoutingDecision`. Side-effect-free routers are
trivially testable and keep the graph topology declarative.
"""

from __future__ import annotations

import logging

from src.core.domain import (
    HealingState,
    RoutingDecision,
    SandboxVerdict,
)

_LOGGER = logging.getLogger(__name__)


def route_after_fix(state: HealingState) -> RoutingDecision:
    """Validate when the Corrector produced a patch, else rollback.

    Under the *apply-in-place* contract the files are already modified by
    the time the Corrector returns a patch. ``None`` means no edit was
    produced, so the retry budget is consumed via rollback.
    """
    patch = state.get("current_patch")
    decision = (
        RoutingDecision.TO_VALIDATION if patch is not None else RoutingDecision.TO_ROLLBACK
    )
    _LOGGER.debug("router.fix", extra={"decision": decision.value, "has_patch": patch is not None})
    return decision


def route_after_validate(state: HealingState) -> RoutingDecision:
    """Immunize on progress, rollback when the same error persists.

    Three outcomes after the reproduction command runs:

    * **green** (verdict PASSED) → the current error is gone → immunize.
    * **a different error** surfaced (``post_fix_signature`` does not match
      the in-progress signature) → progress on a chained error → immunize
      and let ``report_commit`` commit + advance.
    * **the same error** (or an unparseable / infra failure) → rollback and
      retry within the per-error budget.
    """
    # A regression (the fix broke a protected test) always retries — committing
    # it would record broken progress. Checked before the green/changed logic.
    if state.get("regression_detected"):
        _LOGGER.info("router.validate", extra={"decision": "to_rollback", "reason": "regression"})
        return RoutingDecision.TO_ROLLBACK
    result = state.get("current_sandbox_result")
    if result is not None and result.verdict is SandboxVerdict.PASSED:
        decision = RoutingDecision.TO_IMMUNIZE
    else:
        post = state.get("post_fix_signature")
        current = state.get("current_error_signature")
        if post is not None and not post.matches(current):
            decision = RoutingDecision.TO_IMMUNIZE  # different error ⇒ progress
        else:
            decision = RoutingDecision.TO_ROLLBACK  # same error / can't tell ⇒ retry
    _LOGGER.info(
        "router.validate",
        extra={
            "decision": decision.value,
            "verdict": result.verdict.value if result else "none",
        },
    )
    return decision


def route_after_commit(state: HealingState) -> RoutingDecision:
    """Continue the chain on a residual error, otherwise finish.

    ``report_commit_node`` sets ``should_continue`` when a different
    (chained) error remains *and* the error-cycle budget allows another
    pass — it also resets the per-error scratch and points the state at the
    new error.  This router merely follows that decision.
    """
    decision = RoutingDecision.TO_FIX if state.get("should_continue") else RoutingDecision.FINISH
    _LOGGER.info(
        "router.commit",
        extra={"decision": decision.value, "cycle": state.get("error_cycle_index", 0)},
    )
    return decision


def route_after_rollback(state: HealingState) -> RoutingDecision:
    """Retry the Corrector while budget remains, otherwise escalate."""
    attempts = state.get("attempt_count", 0)
    limit = state.get("max_retries", 3)
    decision = (
        RoutingDecision.TO_FIX if attempts < limit else RoutingDecision.TO_POST_MORTEM
    )
    _LOGGER.info(
        "router.rollback",
        extra={"attempts": attempts, "limit": limit, "decision": decision.value},
    )
    return decision


__all__ = [
    "route_after_commit",
    "route_after_fix",
    "route_after_rollback",
    "route_after_validate",
]
