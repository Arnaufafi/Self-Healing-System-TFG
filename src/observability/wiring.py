"""Composition-root helper: wrap a Dependencies bundle with telemetry.

Kept out of ``observability/__init__`` on purpose — it imports
:class:`Dependencies` from the orchestrator, so re-exporting it from the
package root would risk an import cycle (orchestrator → graph → observability).
Import it explicitly at the composition root (``main.py`` / the benchmark).
"""

from __future__ import annotations

from dataclasses import replace

from src.core.ports import TelemetryPort
from src.observability.agents import (
    InstrumentedFixer,
    InstrumentedReporterAgent,
    InstrumentedTester,
)
from src.observability.infrastructure import InstrumentedGit, InstrumentedSandbox
from src.observability.llm import ensure_registered
from src.orchestrator.dependencies import Dependencies


def instrument_dependencies(deps: Dependencies, sink: TelemetryPort) -> Dependencies:
    """Return a copy of *deps* with every port wrapped to emit spans to *sink*.

    Node-level spans are added separately by :func:`~src.orchestrator.build_graph`
    (it reads ``deps.telemetry``), so this single call instruments the whole
    pipeline: the five collaborator ports here, plus the seven graph nodes there.
    Also registers the litellm callback (idempotent) so token/cost is captured;
    route those spans with ``using_llm_sink(sink)`` around the run.
    """
    ensure_registered()
    return replace(
        deps,
        fixer=InstrumentedFixer(deps.fixer, sink),
        tester=InstrumentedTester(deps.tester, sink),
        reporter_agent=InstrumentedReporterAgent(deps.reporter_agent, sink),
        sandbox=InstrumentedSandbox(deps.sandbox, sink),
        git=InstrumentedGit(deps.git, sink),
        telemetry=sink,
    )
