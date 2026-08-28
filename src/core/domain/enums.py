"""Domain enumerations used across the self-healing pipeline.

These enums provide a strongly typed vocabulary for trigger sources,
sandbox verdicts and conditional routing decisions inside the LangGraph
state machine. Centralising them here avoids the proliferation of
"magic strings" through the codebase and makes the graph topology
self-documenting.
"""

from __future__ import annotations

from enum import StrEnum


class TriggerType(StrEnum):
    """Origin of the self-healing cycle.

    The orchestrator routes the initial state to either the QA agent
    (Crash Driven Development entry point) or directly to the Dev agent
    (Test Driven entry point) based on this value.
    """

    PRODUCTION_CRASH = "production_crash"
    TEST_FAILURE = "test_failure"


class SandboxVerdict(StrEnum):
    """Outcome of executing a patched workspace inside the sandbox."""

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class RoutingDecision(StrEnum):
    """Decisions emitted by conditional edges in the graph.

    Using a closed enum (instead of free-form strings) gives static
    analysers a chance to detect dead edges at review time.
    """

    TO_FIX = "to_fix"                # (re)enter the Corrector
    TO_VALIDATION = "to_validation"  # run the reproduction command
    TO_IMMUNIZE = "to_immunize"      # error cleared → write/append regression test
    TO_ROLLBACK = "to_rollback"      # same error persists → revert + retry
    TO_POST_MORTEM = "to_post_mortem"
    FINISH = "finish"                # session complete (END)
