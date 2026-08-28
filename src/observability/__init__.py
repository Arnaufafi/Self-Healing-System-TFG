"""Observability: decorator-based telemetry for the hexagonal ports.

The business adapters (Corrector, Tester, Reporter, sandbox, git) stay free of
timing/metrics code; instead each port is wrapped by a thin GoF *decorator*
that records a :class:`~src.core.domain.Span` around the call and delegates to
the real adapter.  Wiring happens once at the composition root via
:func:`src.observability.wiring.instrument_dependencies`, so the tests and the
mock demo run un-instrumented.

``wiring`` is intentionally NOT re-exported here (it imports the orchestrator's
``Dependencies``); import it explicitly where you compose the app.
"""

from __future__ import annotations

from src.observability.agents import (
    InstrumentedFixer,
    InstrumentedReporterAgent,
    InstrumentedTester,
)
from src.observability.infrastructure import InstrumentedGit, InstrumentedSandbox
from src.observability.llm import ensure_registered, use_agent, using_llm_sink
from src.observability.sinks import (
    InMemoryTelemetry,
    JsonlTelemetry,
    MultiTelemetry,
    NullTelemetry,
)
from src.observability.span import instrument_node, span

__all__ = [
    "InMemoryTelemetry",
    "InstrumentedFixer",
    "InstrumentedGit",
    "InstrumentedReporterAgent",
    "InstrumentedSandbox",
    "InstrumentedTester",
    "JsonlTelemetry",
    "MultiTelemetry",
    "NullTelemetry",
    "ensure_registered",
    "instrument_node",
    "span",
    "use_agent",
    "using_llm_sink",
]
