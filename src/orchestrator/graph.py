"""LangGraph orchestrator assembly (fix-first pipeline).

This module owns the topology of the self-healing pipeline. It binds
every node from :mod:`src.orchestrator.nodes` to its routing function
from :mod:`src.orchestrator.routers`, injects the :class:`Dependencies`
container via ``functools.partial``, and compiles the graph with a
:class:`MemorySaver` checkpointer.

Topology::

    START → bootstrap → fix ─patch→ validate ─green/changed→ immunize → report_commit
                          │ no patch        │ same error            │
                          ▼                 ▼                       ├─residual→ fix
                       rollback ◀───────────┘                       └─done→ END
                          │ retries left → fix
                          │ exhausted    → post_mortem → END
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.core.domain import HealingState, RoutingDecision
from src.observability import instrument_node
from src.orchestrator.dependencies import Dependencies
from src.orchestrator.nodes import (
    bootstrap_node,
    fix_node,
    immunize_node,
    post_mortem_node,
    report_commit_node,
    rollback_node,
    validate_node,
)
from src.orchestrator.routers import (
    route_after_commit,
    route_after_fix,
    route_after_rollback,
    route_after_validate,
)

if TYPE_CHECKING:  # pragma: no cover
    from langgraph.graph.state import CompiledStateGraph

_LOGGER = logging.getLogger(__name__)

# Canonical node names. Exposed so tests can reference them safely.
NODE_BOOTSTRAP = "bootstrap"
NODE_FIX = "fix"
NODE_VALIDATE = "validate"
NODE_IMMUNIZE = "immunize"
NODE_REPORT_COMMIT = "report_commit"
NODE_ROLLBACK = "rollback"
NODE_POST_MORTEM = "post_mortem"


def build_graph(
    deps: Dependencies,
    *,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    """Assemble and compile the self-healing :class:`StateGraph`.

    Args:
        deps: Fully wired :class:`Dependencies` container.
        checkpointer: Optional pre-built checkpointer. When ``None`` a
            fresh :class:`MemorySaver` is created — adequate for tests
            and the TFG demo, but production must inject a persistent
            backend.

    Returns:
        A compiled graph ready to be invoked with ``ainvoke`` /
        ``astream`` and a ``configurable.thread_id``.
    """
    if checkpointer is None:
        checkpointer = MemorySaver()
        _LOGGER.info("graph.build.default_checkpointer", extra={"type": "MemorySaver"})

    builder = StateGraph(HealingState)

    # --- Bind dependencies once via partial -----------------------------
    _bootstrap = functools.partial(bootstrap_node, deps=deps)
    _fix = functools.partial(fix_node, deps=deps)
    _validate = functools.partial(validate_node, deps=deps)
    _immunize = functools.partial(immunize_node, deps=deps)
    _report_commit = functools.partial(report_commit_node, deps=deps)
    _rollback = functools.partial(rollback_node, deps=deps)
    _post_mortem = functools.partial(post_mortem_node, deps=deps)

    # --- Register async nodes (each wrapped to emit a "node.<name>" span) -
    tel = deps.telemetry
    builder.add_node(NODE_BOOTSTRAP, instrument_node(NODE_BOOTSTRAP, _bootstrap, tel))
    builder.add_node(NODE_FIX, instrument_node(NODE_FIX, _fix, tel))
    builder.add_node(NODE_VALIDATE, instrument_node(NODE_VALIDATE, _validate, tel))
    builder.add_node(NODE_IMMUNIZE, instrument_node(NODE_IMMUNIZE, _immunize, tel))
    builder.add_node(NODE_REPORT_COMMIT, instrument_node(NODE_REPORT_COMMIT, _report_commit, tel))
    builder.add_node(NODE_ROLLBACK, instrument_node(NODE_ROLLBACK, _rollback, tel))
    builder.add_node(NODE_POST_MORTEM, instrument_node(NODE_POST_MORTEM, _post_mortem, tel))

    # --- Topology --------------------------------------------------------
    builder.add_edge(START, NODE_BOOTSTRAP)
    # Both entry kinds go straight to the Corrector (fix-first).
    builder.add_edge(NODE_BOOTSTRAP, NODE_FIX)

    # Fix → Validation (patch produced) | Rollback (no patch)
    builder.add_conditional_edges(
        NODE_FIX,
        route_after_fix,
        {
            RoutingDecision.TO_VALIDATION.value: NODE_VALIDATE,
            RoutingDecision.TO_ROLLBACK.value: NODE_ROLLBACK,
        },
    )

    # Validation → Immunize (green/changed) | Rollback (same error)
    builder.add_conditional_edges(
        NODE_VALIDATE,
        route_after_validate,
        {
            RoutingDecision.TO_IMMUNIZE.value: NODE_IMMUNIZE,
            RoutingDecision.TO_ROLLBACK.value: NODE_ROLLBACK,
        },
    )

    # Immunize → Report+Commit (always; immunize is a no-op on test entry)
    builder.add_edge(NODE_IMMUNIZE, NODE_REPORT_COMMIT)

    # Report+Commit → Fix (chained error remains) | END (session complete)
    builder.add_conditional_edges(
        NODE_REPORT_COMMIT,
        route_after_commit,
        {
            RoutingDecision.TO_FIX.value: NODE_FIX,
            RoutingDecision.FINISH.value: END,
        },
    )

    # Rollback → Fix (retries left) | Post-Mortem (exhausted)
    builder.add_conditional_edges(
        NODE_ROLLBACK,
        route_after_rollback,
        {
            RoutingDecision.TO_FIX.value: NODE_FIX,
            RoutingDecision.TO_POST_MORTEM.value: NODE_POST_MORTEM,
        },
    )

    # Terminal node.
    builder.add_edge(NODE_POST_MORTEM, END)

    return builder.compile(checkpointer=checkpointer)


__all__ = [
    "NODE_BOOTSTRAP",
    "NODE_FIX",
    "NODE_IMMUNIZE",
    "NODE_POST_MORTEM",
    "NODE_REPORT_COMMIT",
    "NODE_ROLLBACK",
    "NODE_VALIDATE",
    "build_graph",
]
