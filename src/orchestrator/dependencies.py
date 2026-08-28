"""Dependency container for the LangGraph orchestrator.

This is the only mutable wiring point of the application. The compose
root (``main.py``) builds a :class:`Dependencies` instance and feeds it
to :func:`build_graph`. Each node closes over this container, so unit
tests can construct ad-hoc bundles with in-memory stubs to exercise a
specific branch in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config import Settings
from src.core.domain import NullTelemetry
from src.core.ports import (
    FixerPort,
    GitPort,
    ReporterAgentPort,
    ReporterPort,
    SandboxPort,
    TelemetryPort,
    TesterPort,
)


@dataclass(frozen=True, slots=True)
class Dependencies:
    """Bundle of collaborators required by the orchestrator nodes.

    Frozen and ``slots=True`` to make accidental mutation a runtime
    error and reduce per-instance memory overhead.

    The three agents map to the fix-first roles:

    * ``fixer``          — the Corrector (clears the error in place).
    * ``tester``         — writes the immunization regression test.
    * ``reporter_agent`` — composes commit messages and post-mortem prose.

    ``reporter`` (filesystem) persists the post-mortem; ``sandbox`` runs the
    reproduction command; ``git`` commits / rolls back.

    ``telemetry`` is the cross-cutting observability sink. It defaults to a
    no-op, so tests and the mock demo run un-instrumented; the composition root
    swaps in a real sink (and wraps the ports) via
    :func:`src.observability.wiring.instrument_dependencies`.
    """

    settings: Settings
    fixer: FixerPort
    tester: TesterPort
    reporter_agent: ReporterAgentPort
    sandbox: SandboxPort
    git: GitPort
    reporter: ReporterPort
    telemetry: TelemetryPort = field(default_factory=NullTelemetry)
