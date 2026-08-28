"""Telemetry value object (cross-cutting observability).

A :class:`Span` is one timed unit of work — an agent call, an infrastructure
call, or a graph node — emitted by the instrumentation decorators in
:mod:`src.observability`.  It is a plain, immutable record so a sink can log
it, aggregate it, or forward it to OpenTelemetry without coupling the core to
any backend.

:class:`NullTelemetry` is the default no-op sink: with it, instrumentation is a
zero-cost wrapper, so production wiring opts in to a real sink while the tests
and the mock demo stay silent.  It lives here (not next to the protocol) and
deliberately does *not* import the port, keeping the domain dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Span:
    """One timed, named unit of work.

    Attributes:
        name: Dotted span name, e.g. ``"fixer.fix"`` or ``"node.validate"``.
        duration_s: Wall-clock duration in seconds.
        status: ``"ok"`` or ``"error"``.
        attributes: Free-form context (incident id, command, verdict, ...).
        error_type: Exception class name when ``status == "error"``.
        timestamp: Unix epoch seconds when the span was recorded.
    """

    name: str
    duration_s: float
    status: str = "ok"
    attributes: dict[str, object] = field(default_factory=dict)
    error_type: str | None = None
    timestamp: float = 0.0


class NullTelemetry:
    """No-op telemetry sink (Null Object). Discards every span.

    Structural match for :class:`~src.core.ports.telemetry.TelemetryPort`
    without importing it, so the domain layer stays dependency-free.
    """

    def record(self, span: Span) -> None:
        """Discard *span*."""
        return None
