"""Composition root for the self-healing pipeline.

Wires concrete adapters into a :class:`Dependencies` container, builds
the graph and runs a single demonstration cycle.  The ``agent_mode``
setting (``CDD_AGENT_MODE`` env var) selects between:

* ``"mock"``  — deterministic in-memory agents, no external tools needed.
* ``"real"``  — mini-swe-agent Corrector + litellm Tester/Reporter +
  real Docker sandbox + real Git.

In production the same ``main()`` would be replaced by a queue
consumer (Kafka, Pub/Sub, ...).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import uuid
from pathlib import Path
from typing import cast

from src.agents import MockFixer, MockReporterAgent, MockTester
from src.config import Settings, configure_logging, load_settings
from src.core.domain import (
    CrashReport,
    HealingState,
    SandboxVerdict,
    TriggerEvent,
    TriggerType,
)
from src.core.ports import (
    FixerPort,
    GitPort,
    ReporterAgentPort,
    SandboxPort,
    TesterPort,
)
from src.infrastructure.docker_sandbox import InMemorySandbox
from src.infrastructure.git_ops import InMemoryGit
from src.infrastructure.persistence import FilesystemReporter
from src.orchestrator import Dependencies, build_graph

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency builders per mode
# ---------------------------------------------------------------------------

def _build_mock_agents(
    settings: Settings,
) -> tuple[FixerPort, TesterPort, ReporterAgentPort]:
    """Wire deterministic offline agents (Corrector / Tester / Reporter)."""
    fixer = MockFixer(
        artificial_latency_s=0.01,
        fail_on_attempts=(0,),  # First attempt fails → exercises rollback.
    )
    tester = MockTester(artificial_latency_s=0.01)
    reporter_agent = MockReporterAgent(artificial_latency_s=0.01)
    return fixer, tester, reporter_agent


def _build_real_agents(
    settings: Settings, workspace: str
) -> tuple[FixerPort, TesterPort, ReporterAgentPort]:
    """Wire the real Corrector (mini-swe-agent), Tester and Reporter (litellm).

    Imports are deferred so the project stays runnable without
    ``mini-swe-agent`` / ``litellm`` installed when in mock mode.
    """
    from src.agents import LLMReporter, LLMTester, MiniSWEFixer

    fixer: FixerPort = MiniSWEFixer(
        model_name=settings.llm_model,
        workspace_path=workspace,
        cost_limit=settings.sweagent_cost_limit,
        timeout_seconds=settings.sweagent_timeout_seconds,
        step_limit=settings.sweagent_step_limit,
        trajectory_dir=settings.sweagent_trajectory_dir or None,
        use_docker=settings.sweagent_use_docker,
        docker_image=settings.sweagent_docker_image,
    )
    tester: TesterPort = LLMTester(
        model_name=settings.llm_model,
        workspace_path=workspace,
        timeout_seconds=settings.tester_timeout_seconds,
    )
    reporter_agent: ReporterAgentPort = LLMReporter(model_name=settings.llm_model)
    return fixer, tester, reporter_agent


def _build_mock_infra(
    settings: Settings,
) -> tuple[SandboxPort, GitPort]:
    """Wire deterministic in-memory infra (no Docker / Git required)."""
    sandbox = InMemorySandbox(
        # 1st attempt: validation fails → rollback exercises the retry path.
        # 2nd attempt: validation passes → reaches consolidation.
        scripted_verdicts=(SandboxVerdict.FAILED, SandboxVerdict.PASSED),
        default_verdict=SandboxVerdict.PASSED,
    )
    git = InMemoryGit()
    return sandbox, git


def _build_real_infra(
    settings: Settings,
) -> tuple[SandboxPort, GitPort]:
    """Wire real Docker sandbox and Git adapter."""
    from src.infrastructure.docker_sandbox import DockerSandbox
    from src.infrastructure.git_ops import GitAdapter

    sandbox = DockerSandbox(settings)
    git = GitAdapter()
    return sandbox, git


def _build_dependencies(settings: Settings, workspace: Path) -> Dependencies:
    """Assemble the full dependency container based on ``agent_mode``."""
    mode = settings.agent_mode

    if mode == "mock":
        fixer, tester, reporter_agent = _build_mock_agents(settings)
        sandbox, git = _build_mock_infra(settings)
    elif mode == "real":
        fixer, tester, reporter_agent = _build_real_agents(settings, str(workspace))
        sandbox, git = _build_real_infra(settings)
    else:
        raise ValueError(f"Unknown agent_mode: {mode!r}. Use 'mock' or 'real'.")

    _LOGGER.info(
        "dependencies.built",
        extra={
            "agent_mode": mode,
            "fixer": type(fixer).__name__,
            "tester": type(tester).__name__,
            "reporter_agent": type(reporter_agent).__name__,
            "sandbox": type(sandbox).__name__,
            "git": type(git).__name__,
        },
    )
    deps = Dependencies(
        settings=settings,
        fixer=fixer,
        tester=tester,
        reporter_agent=reporter_agent,
        sandbox=sandbox,
        git=git,
        reporter=FilesystemReporter(settings),
    )
    # Cross-cutting telemetry via the decorator pattern: wrap every port (and,
    # in build_graph, every node) so timings are captured without touching the
    # business adapters. Defaults to an in-memory sink.
    from src.observability import InMemoryTelemetry
    from src.observability.wiring import instrument_dependencies

    return instrument_dependencies(deps, InMemoryTelemetry())


# ---------------------------------------------------------------------------
# Demo state builder
# ---------------------------------------------------------------------------

def _build_initial_state(workspace: Path) -> HealingState:
    """Construct a synthetic crash to drive the demo."""
    crash = CrashReport(
        incident_id=f"demo-{uuid.uuid4().hex[:8]}",
        service_name="billing-api",
        stack_trace=(
            "Traceback (most recent call last):\n"
            '  File "src/billing.py", line 42, in compute_total\n'
            "    return sum(items)\n"
            "TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
        ),
        commit_sha="0123456789abcdef",
    )
    trigger = TriggerEvent(trigger_type=TriggerType.PRODUCTION_CRASH, crash_report=crash)
    return HealingState(
        trigger=trigger,
        workspace_path=str(workspace),
        attempt_count=0,
        failed_attempts=[],
        logs=[],
        is_resolved=False,
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def run_demo(workspace: Path) -> HealingState:
    """Run a single end-to-end self-healing cycle and return final state."""
    settings = load_settings()
    configure_logging(level=settings.log_level, json_mode=settings.log_json)

    deps = _build_dependencies(settings, workspace)
    graph = build_graph(deps)
    initial_state = _build_initial_state(workspace)

    thread_id = f"thread-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    _LOGGER.info("demo.start", extra={"thread_id": thread_id})

    from src.observability.llm import using_llm_sink

    with using_llm_sink(deps.telemetry):
        final_state = cast(HealingState, await graph.ainvoke(initial_state, config=config))

    _LOGGER.info(
        "demo.done",
        extra={
            "resolved": final_state.get("is_resolved", False),
            "attempts": final_state.get("attempt_count", 0),
            "post_mortem": final_state.get("post_mortem_path"),
        },
    )
    return final_state


def main() -> int:
    """CLI entry point.

    The demo runs the mock pipeline inside a throwaway temporary workspace so
    the immunization step (which appends a regression test to the session
    file) never litters the real repository.
    """
    settings = load_settings()
    configure_logging(level=settings.log_level, json_mode=settings.log_json)
    _LOGGER.info("main.start", extra={"agent_mode": settings.agent_mode})
    try:
        with tempfile.TemporaryDirectory(prefix="selfheal-demo-") as tmp:
            asyncio.run(run_demo(Path(tmp).resolve()))
    except RuntimeError as exc:
        _LOGGER.error("demo.fatal", extra={"error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
