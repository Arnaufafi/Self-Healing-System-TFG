"""GoF decorators that instrument the agent ports (Corrector, Tester, Reporter).

Each wraps an inner port implementation, records a :class:`Span` around the
call, and delegates verbatim — the wrapped adapter keeps zero telemetry code.
The ``use_agent`` context also tags every litellm completion made during the
call (see :mod:`src.observability.llm`) so token/cost is attributable per agent.
"""

from __future__ import annotations

from src.core.domain import (
    FailedAttempt,
    FixContext,
    Patch,
    RegressionTest,
    ResolvedError,
    TriggerEvent,
)
from src.core.ports import FixerPort, ReporterAgentPort, TelemetryPort, TesterPort
from src.observability.llm import use_agent
from src.observability.span import span


class InstrumentedFixer(FixerPort):
    """Times :meth:`FixerPort.fix` and tags whether a patch was produced."""

    def __init__(self, inner: FixerPort, sink: TelemetryPort) -> None:
        """Wrap *inner*, recording spans to *sink*."""
        self._inner = inner
        self._sink = sink

    async def fix(self, context: FixContext) -> Patch | None:
        """See :meth:`FixerPort.fix`."""
        with use_agent("corrector"):
            async with span(self._sink, "fixer.fix", incident_id=context.incident_id) as attrs:
                patch = await self._inner.fix(context)
                attrs["produced_patch"] = patch is not None
                return patch


class InstrumentedTester(TesterPort):
    """Times :meth:`TesterPort.write_regression_test`."""

    def __init__(self, inner: TesterPort, sink: TelemetryPort) -> None:
        """Wrap *inner*, recording spans to *sink*."""
        self._inner = inner
        self._sink = sink

    async def write_regression_test(self, context: FixContext) -> RegressionTest:
        """See :meth:`TesterPort.write_regression_test`."""
        with use_agent("tester"):
            async with span(
                self._sink, "tester.write_regression_test", incident_id=context.incident_id
            ):
                return await self._inner.write_regression_test(context)


class InstrumentedReporterAgent(ReporterAgentPort):
    """Times both narrative calls of :class:`ReporterAgentPort`."""

    def __init__(self, inner: ReporterAgentPort, sink: TelemetryPort) -> None:
        """Wrap *inner*, recording spans to *sink*."""
        self._inner = inner
        self._sink = sink

    async def compose_commit_message(
        self,
        *,
        incident_id: str,
        error_signature: str,
        diff_text: str,
        prior_attempts: tuple[str, ...],
        test_path: str | None,
    ) -> str:
        """See :meth:`ReporterAgentPort.compose_commit_message`."""
        with use_agent("reporter"):
            async with span(
                self._sink, "reporter.compose_commit_message", incident_id=incident_id
            ):
                return await self._inner.compose_commit_message(
                    incident_id=incident_id,
                    error_signature=error_signature,
                    diff_text=diff_text,
                    prior_attempts=prior_attempts,
                    test_path=test_path,
                )

    async def compose_post_mortem(
        self,
        *,
        incident_id: str,
        trigger: TriggerEvent,
        attempts: tuple[FailedAttempt, ...],
        resolved: tuple[ResolvedError, ...],
    ) -> str:
        """See :meth:`ReporterAgentPort.compose_post_mortem`."""
        with use_agent("reporter"):
            async with span(
                self._sink, "reporter.compose_post_mortem", incident_id=incident_id
            ):
                return await self._inner.compose_post_mortem(
                    incident_id=incident_id,
                    trigger=trigger,
                    attempts=attempts,
                    resolved=resolved,
                )
