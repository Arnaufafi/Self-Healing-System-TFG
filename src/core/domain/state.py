"""LangGraph state definition with reducer-aware accumulators.

LangGraph requires the state schema to be a ``TypedDict`` (or compatible)
so it can introspect annotated reducers via ``typing.Annotated``. We
therefore expose the canonical state shape here, while reusing the
Pydantic value objects from :mod:`src.core.domain.models` for every
field that benefits from validation.

The shape has two layers:

* **Session-level** fields set once by ``bootstrap_node`` and stable for the
  whole run (``session_id``, ``reproduce_cmd``, ``regression_test_path``,
  budgets).
* **Current-error** fields that are *reset* as the outer loop advances from
  one chained error to the next (``current_error_signature``,
  ``current_failure_output``, ``attempt_count`` ...).

Three accumulators use ``operator.add`` so each node returns a delta and
LangGraph merges them: ``failed_attempts``, ``resolved_errors`` and
``logs``.
"""

from __future__ import annotations

import operator
from typing import Annotated, Required, TypedDict

from src.core.domain.models import (
    CrashReport,
    FailedAttempt,
    FailingTest,
    Patch,
    RegressionTest,
    ResolvedError,
    SandboxResult,
    TriggerEvent,
)
from src.core.domain.signature import ErrorKind, ErrorSignature


class HealingState(TypedDict, total=False):
    """Canonical mutable state propagated by the LangGraph orchestrator.

    Fields are ``total=False`` because nodes return partial updates merged
    into the running state. Consumers must guard reads with ``.get()``.
    """

    # --- Entry point (Required — always present from state construction) ---
    trigger: Required[TriggerEvent]
    workspace_path: Required[str]

    # --- Session-level (set once by bootstrap_node) -----------------------
    session_id: str
    entry_kind: ErrorKind
    reproduce_cmd: tuple[str, ...]
    # No-regression gate (protected-tests command):
    #   * SWE-bench: set explicitly to ``pytest <PASS_TO_PASS>``.
    #   * general:  left unset → validate selects the tests related to the files
    #     the fix touched (by import + name) and requires them to stay green.
    regression_cmd: tuple[str, ...]
    regression_test_path: str
    max_retries: int
    error_cycle_budget: int
    sandbox_image: str

    # --- Derived from trigger (populated by bootstrap_node) ---------------
    crash_report: CrashReport | None
    failing_test: FailingTest | None

    # --- Current error under repair (reset when advancing the chain) ------
    current_incident_id: str
    current_error_signature: ErrorSignature | None
    current_failure_output: str

    # --- Per-iteration artefacts -----------------------------------------
    current_patch: Patch | None
    current_sandbox_result: SandboxResult | None
    current_regression_test: RegressionTest | None
    # Set by validate_node: the error observed AFTER the fix ran (None when
    # the reproduction command went green). route_after_validate compares it
    # against ``current_error_signature`` to tell retry from progress.
    post_fix_signature: ErrorSignature | None
    post_fix_output: str
    # Set by validate_node: True when the fix cleared the target test but broke a
    # protected one (regression_cmd). route_after_validate forces a rollback.
    regression_detected: bool
    # Set by report_commit_node: True when a different (chained) error remains
    # and budget allows another cycle. route_after_commit reads it.
    should_continue: bool

    # --- Retry / chain bookkeeping ---------------------------------------
    attempt_count: int          # reintentos del error actual (reset al avanzar)
    error_cycle_index: int      # índice del error encadenado en curso

    # --- History accumulators (reducer = list concatenation) -------------
    failed_attempts: Annotated[list[FailedAttempt], operator.add]
    resolved_errors: Annotated[list[ResolvedError], operator.add]
    logs: Annotated[list[str], operator.add]

    # --- Terminal flags ---------------------------------------------------
    is_resolved: bool
    post_mortem_path: str | None
