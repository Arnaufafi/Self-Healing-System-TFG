"""Strongly typed value objects exchanged between graph nodes.

All models are immutable Pydantic ``BaseModel`` subclasses (``frozen=True``)
so they can be safely shared across asynchronous tasks without race
conditions on mutable references. Every field carries a ``Field``
description to keep the OpenAPI / JSON-Schema export useful for the
report annexes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from src.core.domain.enums import (
    SandboxVerdict,
    TriggerType,
)
from src.core.domain.signature import ErrorSignature


class _FrozenModel(BaseModel):
    """Common base imposing immutability and strict validation."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class _CodeFrozenModel(BaseModel):
    """Immutable base that preserves source-code whitespace verbatim.

    ``str_strip_whitespace`` is deliberately **off**: leading indentation
    and trailing newlines are load-bearing in Python source, diff hunks and
    captured tracebacks.  Used by the value objects that carry code or raw
    failure text (:class:`SourceExcerpt`, :class:`FixContext`,
    :class:`RegressionTest`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class CrashReport(_FrozenModel):
    """Incoming signal from production observability stacks.

    The stack trace and originating service are sufficient context for
    the QA agent to derive a regression test. Additional metadata (build
    hash, request id, etc.) can be threaded through ``extra_context``
    without breaking the schema.
    """

    incident_id: str = Field(..., description="Unique identifier of the incident.")
    service_name: str = Field(..., description="Logical name of the failing service.")
    stack_trace: str = Field(..., description="Raw stack trace captured at crash time.")
    commit_sha: str = Field(..., description="SHA of the deployed revision.")
    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp at which the crash was captured.",
    )


class FailingTest(_FrozenModel):
    """Representation of a failing pytest case."""

    node_id: str = Field(..., description="Pytest node identifier (``path::test_fn``).")
    source: str = Field(..., description="Source code of the failing test.")
    last_failure_output: str = Field(..., description="Captured stdout/stderr of the run.")


class Patch(_FrozenModel):
    """Unified diff produced by the Dev agent."""

    diff_text: str = Field(..., description="Raw unified diff content.")
    author_agent: str = Field(..., description="Identifier of the agent that produced the patch.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC creation timestamp.",
    )


class SandboxResult(_FrozenModel):
    """Result of executing the validation suite inside the sandbox."""

    verdict: SandboxVerdict = Field(..., description="Symbolic verdict of the run.")
    exit_code: int | None = Field(
        default=None,
        description="Exit code of the test command, ``None`` on infra errors.",
    )
    duration_seconds: float = Field(..., ge=0.0, description="Wall-clock duration.")
    logs_tail: str = Field(
        default="",
        description="Last N kilobytes of combined stdout/stderr, redacted.",
    )


class FailedAttempt(_FrozenModel):
    """Record of one failed self-healing cycle.

    Instances are accumulated through a LangGraph reducer (``operator.add``)
    so the post-mortem node can render the full history in chronological
    order.
    """

    attempt_index: int = Field(
        ..., ge=0, description="Zero-based attempt counter (within the error cycle)."
    )
    cycle_index: int = Field(
        default=0, ge=0, description="Index of the chained-error cycle this attempt belongs to."
    )
    patch: Patch | None = Field(default=None, description="Patch that was tried.")
    sandbox_result: SandboxResult | None = Field(
        default=None, description="Outcome of the sandbox validation."
    )
    error_summary: str = Field(..., description="Human-readable reason for the failure.")
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp of the record.",
    )


class TriggerEvent(_FrozenModel):
    """Discriminated union-like envelope for graph entry points.

    Exactly one of ``crash_report`` or ``failing_test`` must be provided
    and it must be consistent with ``trigger_type``. Validation is
    enforced by the model validator.
    """

    trigger_type: TriggerType = Field(..., description="Origin of the cycle.")
    crash_report: CrashReport | None = Field(default=None)
    failing_test: FailingTest | None = Field(default=None)

    def model_post_init(self, __context: object) -> None:
        """Validate the discriminated union invariant after construction."""
        if self.trigger_type is TriggerType.PRODUCTION_CRASH and self.crash_report is None:
            raise ValueError("PRODUCTION_CRASH trigger requires a CrashReport payload.")
        if self.trigger_type is TriggerType.TEST_FAILURE and self.failing_test is None:
            raise ValueError("TEST_FAILURE trigger requires a FailingTest payload.")


# ---------------------------------------------------------------------------
# Fix-first value objects (Corrector / Tester / Reporter).
# ---------------------------------------------------------------------------


class SourceExcerpt(_CodeFrozenModel):
    """A workspace file (or slice of one) handed to an agent as context."""

    file_path: str = Field(..., description="Workspace-relative path of the source file.")
    content: str = Field(..., description="Current contents (possibly truncated) of the file.")


class FixContext(_CodeFrozenModel):
    """Uniform input for the Corrector and the Tester.

    The same envelope describes both entry modes: a production crash (raw
    ``failure_output`` is a traceback, ``reproducer_node_id`` is ``None``)
    and a failing test (``failure_output`` is pytest output and
    ``reproducer_node_id`` targets the test).  ``reproduce_cmd`` is the argv
    the sandbox runs to decide whether the error is gone.
    """

    incident_id: str = Field(..., description="Stable identifier of the incident under repair.")
    failure_output: str = Field(
        ..., description="Crash traceback or pytest failure that triggered the cycle."
    )
    reproduce_cmd: tuple[str, ...] = Field(
        ..., description="Argv the sandbox runs to reproduce/verify."
    )
    reproducer_node_id: str | None = Field(
        default=None, description="Pytest node id when the entry was a failing test."
    )
    source_excerpts: tuple[SourceExcerpt, ...] = Field(
        default=(), description="Workspace files pre-loaded as context for the agent."
    )
    previous_attempts: tuple[str, ...] = Field(
        default=(), description="Pytest/crash tails of prior failed attempts (chronological)."
    )
    fix_diff: str = Field(
        default="",
        description="Unified diff of the fix just applied — context for the Tester.",
    )


class RegressionTest(_CodeFrozenModel):
    """Immunization test produced by the Tester after a green fix."""

    path: str = Field(..., description="Session regression file path (workspace-relative).")
    node_id: str = Field(..., description="Pytest node id of the appended test.")
    source: str = Field(..., description="Pytest source of the regression test (one function).")


class ResolvedError(_FrozenModel):
    """One error healed during a session, recorded for the final report.

    Accumulated through a LangGraph reducer (``operator.add``) so a session
    that heals a chain of errors carries the full, ordered ledger.
    """

    signature: ErrorSignature = Field(..., description="Fingerprint of the resolved error.")
    commit_sha: str = Field(..., description="SHA of the commit that healed it.")
    test_path: str | None = Field(
        default=None, description="Regression test file, when one was written."
    )
    resolved_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp of resolution.",
    )
