"""End-to-end smoke tests for the fix-first orchestrator.

Uses the in-memory adapters so the suite is fully hermetic. The tests are
skipped if ``langgraph`` is not importable (e.g. on documentation builds).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

langgraph = pytest.importorskip("langgraph")

from src.agents import MockFixer, MockReporterAgent, MockTester  # noqa: E402
from src.config import Settings  # noqa: E402
from src.core.domain import (  # noqa: E402
    CrashReport,
    FailingTest,
    HealingState,
    SandboxResult,
    SandboxVerdict,
    TriggerEvent,
    TriggerType,
)
from src.infrastructure.docker_sandbox import InMemorySandbox  # noqa: E402
from src.infrastructure.git_ops import InMemoryGit  # noqa: E402
from src.infrastructure.persistence import FilesystemReporter  # noqa: E402
from src.orchestrator import Dependencies, build_graph  # noqa: E402

# Two distinct crash tracebacks whose signatures differ (type + location).
_CRASH_A = (
    "Traceback (most recent call last):\n"
    '  File "main.py", line 5, in run\n'
    "    total = compute(data)\n"
    "TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
)
_CRASH_B = (
    "Traceback (most recent call last):\n"
    '  File "svc.py", line 9, in save\n'
    "    persist(obj)\n"
    "ValueError: invalid record id\n"
)


def _make_deps(
    *,
    reports_dir: Path,
    fail_on_attempts: tuple[int, ...] = (),
    verdicts: tuple[SandboxVerdict, ...] = (),
    results: tuple[SandboxResult, ...] = (),
    default_verdict: SandboxVerdict = SandboxVerdict.PASSED,
    max_retries: int = 3,
    git: InMemoryGit | None = None,
) -> Dependencies:
    settings = Settings(max_retries=max_retries, reports_dir=reports_dir, log_json=False)
    return Dependencies(
        settings=settings,
        fixer=MockFixer(fail_on_attempts=fail_on_attempts),
        tester=MockTester(),
        reporter_agent=MockReporterAgent(),
        sandbox=InMemorySandbox(
            scripted_verdicts=verdicts,
            scripted_results=results,
            default_verdict=default_verdict,
        ),
        git=git or InMemoryGit(),
        reporter=FilesystemReporter(settings),
    )


def _crash_state(workspace: Path, stack_trace: str = _CRASH_A) -> HealingState:
    crash = CrashReport(
        incident_id="inc-it-001", service_name="svc", stack_trace=stack_trace, commit_sha="abc123"
    )
    trigger = TriggerEvent(trigger_type=TriggerType.PRODUCTION_CRASH, crash_report=crash)
    return HealingState(
        trigger=trigger, workspace_path=str(workspace),
        failed_attempts=[], resolved_errors=[], logs=[], is_resolved=False,
    )


def _test_failure_state(workspace: Path) -> HealingState:
    failing = FailingTest(
        node_id="tests/test_x.py::test_y",
        source="def test_y():\n    assert add(1, 2) == 3\n",
        last_failure_output=(
            "tests/test_x.py:2: in test_y\n"
            "E   AssertionError: assert 0 == 3\n"
            "FAILED tests/test_x.py::test_y - AssertionError: assert 0 == 3\n"
        ),
    )
    trigger = TriggerEvent(trigger_type=TriggerType.TEST_FAILURE, failing_test=failing)
    return HealingState(
        trigger=trigger, workspace_path=str(workspace),
        failed_attempts=[], resolved_errors=[], logs=[], is_resolved=False,
    )


def _count_tests(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("def test_") if path.exists() else 0


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.asyncio
async def test_crash_happy_path_immunizes_and_commits(tmp_path: Path) -> None:
    """crash → fix → green → immunize (writes test) → commit → END."""
    git = InMemoryGit()
    deps = _make_deps(reports_dir=tmp_path / "reports", verdicts=(SandboxVerdict.PASSED,), git=git)
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _crash_state(tmp_path), config={"configurable": {"thread_id": "t-happy"}}
    )

    assert final.get("is_resolved") is True
    assert final.get("post_mortem_path") is None
    assert len(final.get("resolved_errors", [])) == 1
    assert len(git.commits) == 1
    # A regression test was appended to the session file.
    rt = final["resolved_errors"][0]
    assert rt.test_path is not None
    session_file = tmp_path / rt.test_path
    assert _count_tests(session_file) == 1


@pytest.mark.asyncio
async def test_immunize_falls_back_to_smoke_when_targeted_test_fails_gate(tmp_path: Path) -> None:
    """When the targeted test fails the gate it is rolled back and the
    deterministic smoke test takes its place — immunization never disappears.

    Validate goes green; the gate of the targeted test fails; the smoke-test
    gate (default verdict) passes, so the committed test is the smoke fallback.
    """
    git = InMemoryGit()
    deps = _make_deps(
        reports_dir=tmp_path / "reports",
        # validate (green); targeted gate (fails); smoke gate → default (passes).
        verdicts=(SandboxVerdict.PASSED, SandboxVerdict.FAILED),
        git=git,
    )
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _crash_state(tmp_path), config={"configurable": {"thread_id": "t-gate-fallback"}}
    )

    assert final.get("is_resolved") is True
    assert len(git.commits) == 1
    # A test IS committed — the smoke fallback, not the rolled-back targeted one.
    assert final.get("current_regression_test") is not None
    assert final["resolved_errors"][0].test_path is not None
    session_file = tmp_path / final["regression_test_path"]
    assert _count_tests(session_file) == 1
    assert "test_reproduce_command_exits_zero" in _read(session_file)


@pytest.mark.asyncio
async def test_pipeline_emits_node_and_agent_telemetry(tmp_path: Path) -> None:
    """Instrumenting the deps captures spans for every node and every port,
    without touching the business adapters."""
    from src.observability import InMemoryTelemetry
    from src.observability.wiring import instrument_dependencies

    tel = InMemoryTelemetry()
    deps = instrument_dependencies(
        _make_deps(reports_dir=tmp_path / "reports", verdicts=(SandboxVerdict.PASSED,)),
        tel,
    )
    graph = build_graph(deps)
    await graph.ainvoke(
        _crash_state(tmp_path), config={"configurable": {"thread_id": "t-tel"}}
    )

    names = {s.name for s in tel.spans}
    # Node-level spans (added in build_graph)…
    assert {"node.bootstrap", "node.fix", "node.validate", "node.immunize"} <= names
    # …and port-level spans (the GoF decorators).
    assert {"fixer.fix", "tester.write_regression_test", "sandbox.run_tests"} <= names
    assert {"reporter.compose_commit_message", "git.commit"} <= names
    # Aggregation produces usable metrics.
    agg = tel.aggregate()
    assert agg["span_count"] >= len(names)
    assert agg["by_name"]["fixer.fix"]["count"] == 1


@pytest.mark.asyncio
async def test_test_failure_entry_skips_immunization(tmp_path: Path) -> None:
    """A failing-test entry is healed without writing a new test."""
    git = InMemoryGit()
    deps = _make_deps(reports_dir=tmp_path / "reports", verdicts=(SandboxVerdict.PASSED,), git=git)
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _test_failure_state(tmp_path), config={"configurable": {"thread_id": "t-test-entry"}}
    )

    assert final.get("is_resolved") is True
    assert final.get("current_regression_test") is None
    assert len(git.commits) == 1
    assert final["resolved_errors"][0].test_path is None
    # No session file at all: an empty stub would pollute the heal commit
    # (it reached real PRs before bootstrap gained the entry-kind guard).
    session_file = tmp_path / final["regression_test_path"]
    assert not session_file.exists()


@pytest.mark.asyncio
async def test_same_error_retry_then_recover(tmp_path: Path) -> None:
    """Same error persists once (rollback) then the retry goes green."""
    git = InMemoryGit()
    deps = _make_deps(
        reports_dir=tmp_path / "reports",
        verdicts=(SandboxVerdict.FAILED, SandboxVerdict.PASSED),  # generic tail ⇒ no sig ⇒ retry
        git=git,
    )
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _crash_state(tmp_path), config={"configurable": {"thread_id": "t-retry"}}
    )

    assert final.get("is_resolved") is True
    assert final.get("attempt_count") == 1
    assert len(final.get("failed_attempts", [])) == 1
    assert len(git.commits) == 1


@pytest.mark.asyncio
async def test_regression_gate_rolls_back_then_recovers(tmp_path: Path) -> None:
    """Fix clears the target test but breaks a protected one (regression_cmd):
    validate rolls back, the retry produces a clean fix, then it commits.

    Sandbox call order (test-entry, immunize is skipped):
    validate#1 reproduce (PASSED) → gate (FAILED) ⇒ regression ⇒ rollback;
    validate#2 reproduce (PASSED) → gate (PASSED) ⇒ commit.
    """
    git = InMemoryGit()
    deps = _make_deps(
        reports_dir=tmp_path / "reports",
        verdicts=(
            SandboxVerdict.PASSED, SandboxVerdict.FAILED,   # attempt 1: target ok, gate broken
            SandboxVerdict.PASSED, SandboxVerdict.PASSED,   # attempt 2: target ok, gate ok
        ),
        git=git,
    )
    state = _test_failure_state(tmp_path)
    state["regression_cmd"] = ("python", "-m", "pytest", "tests/test_protected.py", "-x")
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        state, config={"configurable": {"thread_id": "t-regression"}}
    )

    assert final.get("is_resolved") is True
    assert final.get("attempt_count") == 1          # one rollback caused by the regression
    assert len(final.get("failed_attempts", [])) == 1
    assert "regression" in final["failed_attempts"][0].error_summary
    assert len(git.commits) == 1


@pytest.mark.asyncio
async def test_chained_two_errors_commits_each(tmp_path: Path) -> None:
    """crash A → fix → (crash B surfaces) → commit A + test A → fix B →
    green → commit B + test B → END. The core chained-error behaviour."""
    git = InMemoryGit()
    deps = _make_deps(
        reports_dir=tmp_path / "reports",
        results=(
            # After fixing A, the program now crashes with a *different* error B.
            SandboxResult(
                verdict=SandboxVerdict.FAILED, exit_code=1, duration_seconds=1.0, logs_tail=_CRASH_B
            ),
            # After fixing B, it runs clean.
            SandboxResult(
                verdict=SandboxVerdict.PASSED, exit_code=0, duration_seconds=1.0, logs_tail=""
            ),
        ),
        git=git,
    )
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _crash_state(tmp_path, _CRASH_A), config={"configurable": {"thread_id": "t-chain"}}
    )

    assert final.get("is_resolved") is True
    resolved = final.get("resolved_errors", [])
    assert len(resolved) == 2, "both chained errors should be committed separately"
    assert resolved[0].signature.exc_type == "TypeError"   # error A
    assert resolved[1].signature.exc_type == "ValueError"  # error B
    assert len(git.commits) == 2
    assert final.get("error_cycle_index") == 1
    # Two regression tests in the single session file.
    session_file = tmp_path / final["regression_test_path"]
    assert _count_tests(session_file) == 2


@pytest.mark.asyncio
async def test_debug_logging_uses_no_reserved_logrecord_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard: DEBUG-level node logs must not pass reserved LogRecord keys.

    Passing e.g. ``extra={"message": ...}`` raises ``KeyError: Attempt to
    overwrite 'message' in LogRecord`` at record creation — but only when
    DEBUG is enabled, which the other hermetic tests do not turn on. This
    test exercises the immunize + report_commit debug logs with DEBUG on so a
    reserved-key regression fails loudly instead of silently breaking real runs.
    """
    caplog.set_level(logging.DEBUG)
    git = InMemoryGit()
    deps = _make_deps(reports_dir=tmp_path / "reports", verdicts=(SandboxVerdict.PASSED,), git=git)
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _crash_state(tmp_path), config={"configurable": {"thread_id": "t-debug"}}
    )
    # If a reserved key were used, the node would have raised before resolving.
    assert final.get("is_resolved") is True
    assert len(git.commits) == 1


@pytest.mark.asyncio
async def test_exhausts_retries_writes_post_mortem(tmp_path: Path) -> None:
    """Every validation fails with the same error → post-mortem after budget."""
    git = InMemoryGit()
    deps = _make_deps(
        reports_dir=tmp_path / "reports",
        default_verdict=SandboxVerdict.FAILED,  # generic tail every time ⇒ same error ⇒ rollback
        max_retries=3,
        git=git,
    )
    graph = build_graph(deps)
    final: HealingState = await graph.ainvoke(
        _crash_state(tmp_path), config={"configurable": {"thread_id": "t-exhaust"}}
    )

    assert final.get("is_resolved") is False
    assert final.get("attempt_count") == 3
    assert len(final.get("failed_attempts", [])) == 3
    assert len(git.commits) == 0
    report_path = final.get("post_mortem_path")
    assert report_path is not None
    content = _read(Path(report_path))
    assert content and "Post-Mortem" in content
