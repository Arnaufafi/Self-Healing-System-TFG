"""Telemetry port.

Hexagonal seam for observability: the instrumentation decorators in
:mod:`src.observability` depend on this protocol, not on a concrete backend
(structured logs, a JSONL file, OpenTelemetry, ...).  Wrapping happens once at
the composition root, so the business adapters never see telemetry code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.domain import Span


@runtime_checkable
class TelemetryPort(Protocol):
    """Sink for telemetry spans.

    ``record`` MUST be cheap and MUST NOT raise: telemetry is a cross-cutting
    concern that can never break the business flow.  Implementations that do
    I/O (files, network) swallow their own errors.
    """

    def record(self, span: Span) -> None:
        """Record one finished span."""
        ...
