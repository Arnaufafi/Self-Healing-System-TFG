"""LangGraph node implementations (fix-first pipeline).

Each public coroutine is a graph node. Nodes are small, pure modulo their
injected collaborators, and return a *partial* :class:`HealingState` that
LangGraph merges into the global state honouring the ``Annotated`` reducers.

Control flow (see :mod:`src.orchestrator.routers` for the edges)::

    bootstrap → fix → validate → immunize → report_commit → (fix | END)
                       │                     ↑
                       └── rollback → (fix | post_mortem)

The Corrector edits in place (fix-first); the Tester immunises a healed
crash with a regression test appended to the session file; the Reporter
writes the commit message and the post-mortem narrative.  The outer loop
heals *chained* errors: when a fix clears the current error but a different
one surfaces, ``report_commit`` commits the progress and re-enters ``fix``.

Design principles:

* **No I/O inside conditionals.** All side effects live in nodes.
* **No exception bubbles up.** Every recoverable failure becomes a state
  field; only programmer errors raise.
* **Each node logs structured events** so the audit trail is reconstructible.
"""

from __future__ import annotations

import ast
import logging
import uuid
from pathlib import Path
from typing import Final

from src.core.domain import (
    ErrorKind,
    ErrorSignature,
    FailedAttempt,
    FixContext,
    HealingState,
    Patch,
    RegressionTest,
    ResolvedError,
    SandboxResult,
    SandboxVerdict,
    TriggerEvent,
    TriggerType,
    parse_error,
)
from src.core.exceptions import FixGenerationError, TestGenerationError
from src.orchestrator.dependencies import Dependencies
from src.orchestrator.regression import changed_source_files, select_related_tests

_LOGGER = logging.getLogger(__name__)

# Fallback reproduction command when none is supplied / derivable.
_DEFAULT_TEST_COMMAND: Final[tuple[str, ...]] = (
    "python", "-m", "pytest", "-x", "--tb=short",
)

# Hard cap on the per-attempt failure tail fed back to the Corrector on retry.
_RETRY_TAIL_BUDGET: Final[int] = 1200


# ---------------------------------------------------------------------------
# Bootstrap.
# ---------------------------------------------------------------------------
async def bootstrap_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Initialise the session and fingerprint the initial error.

    Sets budgets, the reproduction command and the session regression file.
    The caller only has to supply ``trigger`` and ``workspace_path`` (and
    optionally ``reproduce_cmd``).
    """
    trigger = state["trigger"]
    workspace = state["workspace_path"]
    session_id = state.get("session_id") or uuid.uuid4().hex[:12]
    entry_kind: ErrorKind = (
        "crash" if trigger.trigger_type is TriggerType.PRODUCTION_CRASH else "test"
    )

    if trigger.crash_report is not None:
        failure_output = trigger.crash_report.stack_trace
        incident_id = trigger.crash_report.incident_id
    elif trigger.failing_test is not None:
        failure_output = trigger.failing_test.last_failure_output
        incident_id = trigger.failing_test.node_id
    else:  # defensive: TriggerEvent validation prevents this
        failure_output, incident_id = "", "unknown"

    reproduce_cmd = tuple(state.get("reproduce_cmd") or ())
    if not reproduce_cmd:
        reproduce_cmd = (
            _DEFAULT_TEST_COMMAND if entry_kind == "test" else ("python", "main.py")
        )

    regression_test_path = (
        state.get("regression_test_path") or f"tests/test_selfheal_{session_id}.py"
    )
    # Only crash-entry immunizes (the Tester appends here); on test-entry the
    # failing CI test IS the regression test, and an empty stub would just
    # pollute the heal commit — it reached real PRs before this guard.
    if entry_kind == "crash":
        _ensure_session_test_file(workspace, regression_test_path, session_id)

    initial_sig = parse_error(failure_output, entry_kind, workspace)

    _LOGGER.info(
        "node.bootstrap",
        extra={"entry_kind": entry_kind, "incident": incident_id, "reproduce": reproduce_cmd},
    )
    update: HealingState = {
        "session_id": session_id,
        "entry_kind": entry_kind,
        "reproduce_cmd": reproduce_cmd,
        "regression_test_path": regression_test_path,
        "max_retries": state.get("max_retries", deps.settings.max_retries),
        "error_cycle_budget": state.get("error_cycle_budget", deps.settings.error_cycle_budget),
        "sandbox_image": state.get("sandbox_image", deps.settings.sandbox_image),
        "current_incident_id": incident_id,
        "current_error_signature": initial_sig,
        "current_failure_output": failure_output,
        "attempt_count": 0,
        "error_cycle_index": 0,
        "is_resolved": False,
        "logs": [f"bootstrap.entry={entry_kind} incident={incident_id}"],
    }
    if trigger.crash_report is not None:
        update["crash_report"] = trigger.crash_report
    if trigger.failing_test is not None:
        update["failing_test"] = trigger.failing_test
    return update


# ---------------------------------------------------------------------------
# Fix (Corrector).
# ---------------------------------------------------------------------------
async def fix_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Edit the working tree in place to clear the current error."""
    ctx = _build_fix_context(state)
    _LOGGER.info(
        "node.fix.start",
        extra={
            "incident": ctx.incident_id,
            "attempt": state.get("attempt_count", 0),
            "cycle": state.get("error_cycle_index", 0),
        },
    )
    try:
        patch: Patch | None = await deps.fixer.fix(ctx)
    except FixGenerationError as exc:
        _LOGGER.exception("node.fix.failed")
        return {"current_patch": None, "logs": [f"fix.error: {exc!s}"]}
    if patch is None:
        return {"current_patch": None, "logs": ["fix.no_patch"]}
    _LOGGER.info("node.fix.done", extra={"diff_bytes": len(patch.diff_text)})
    return {"current_patch": patch, "logs": [f"fix.patch: {len(patch.diff_text)} bytes"]}


# ---------------------------------------------------------------------------
# Validate (sandboxed reproduction).
# ---------------------------------------------------------------------------
async def validate_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Run the reproduction command in the sandbox and fingerprint the result.

    Besides the verdict, this extracts ``post_fix_signature`` — the error
    observed *after* the fix (``None`` when the command went green).
    ``route_after_validate`` compares it against the in-progress signature to
    tell *same error* (retry) from *different error* (progress).
    """
    workspace = state.get("workspace_path", "")
    image = state.get("sandbox_image", deps.settings.sandbox_image)
    cmd = tuple(state.get("reproduce_cmd") or ()) or _DEFAULT_TEST_COMMAND
    if not workspace:
        result = SandboxResult(
            verdict=SandboxVerdict.INFRASTRUCTURE_ERROR,
            duration_seconds=0.0,
            logs_tail="missing workspace_path",
        )
        return {
            "current_sandbox_result": result,
            "post_fix_signature": None,
            "post_fix_output": "",
            "logs": ["validate.invalid_state"],
        }
    _LOGGER.info("node.validate.start", extra={"workspace": workspace, "cmd": cmd})
    result = await deps.sandbox.run_tests(workspace_path=workspace, image=image, command=cmd)
    entry_kind = state.get("entry_kind", "crash")

    # No-regression gate (opt-in): when the reproduction went green, make sure the
    # fix did not break tests that were already passing (e.g. SWE-bench
    # PASS_TO_PASS). A broken protected test is a *regression* — treat it as a
    # failed attempt so the loop rolls back and retries instead of committing it.
    # Only active when ``regression_cmd`` is set; other modes are unaffected.
    regression_cmd = tuple(state.get("regression_cmd") or ())
    if result.verdict is SandboxVerdict.PASSED:
        # No-regression gate: SWE-bench supplies ``regression_cmd`` (PASS_TO_PASS);
        # otherwise select the tests related to the files THIS fix touched and
        # require them to stay green. A break here is a regression → retry.
        if not regression_cmd:
            regression_cmd = _related_tests_cmd(state, workspace)
        if regression_cmd:
            gate = await deps.sandbox.run_tests(
                workspace_path=workspace, image=image, command=regression_cmd
            )
            # pytest exit code 5 = "no tests collected" — nothing to break, so it
            # is not a regression (a related file may legitimately hold no tests).
            if gate.verdict is not SandboxVerdict.PASSED and gate.exit_code != 5:
                _LOGGER.warning(
                    "node.validate.regression",
                    extra={"gate_verdict": gate.verdict.value, "exit_code": gate.exit_code},
                )
                return {
                    "current_sandbox_result": gate,
                    "post_fix_signature": None,
                    "post_fix_output": "",
                    "regression_detected": True,
                    "logs": [f"validate.regression(gate={gate.verdict.value})"],
                }

    post_sig = (
        None
        if result.verdict is SandboxVerdict.PASSED
        else parse_error(result.logs_tail, entry_kind, workspace)
    )
    _LOGGER.info(
        "node.validate.done",
        extra={"verdict": result.verdict.value, "post_sig": str(post_sig) if post_sig else None},
    )
    return {
        "current_sandbox_result": result,
        "post_fix_signature": post_sig,
        "post_fix_output": result.logs_tail,
        "regression_detected": False,
        "logs": [f"validate.verdict={result.verdict.value}"],
    }


def _related_tests_cmd(state: HealingState, workspace: str) -> tuple[str, ...]:
    """Build a pytest command over the tests related to the files the fix touched.

    The general no-regression gate (SWE-bench supplies ``regression_cmd``
    explicitly instead). Returns ``()`` when there is no diff or no related test,
    so the gate stays disabled rather than guessing.
    """
    patch = state.get("current_patch")
    if patch is None or not patch.diff_text:
        return ()
    related = select_related_tests(workspace, changed_source_files(patch.diff_text))
    if not related:
        return ()
    interpreter = (tuple(state.get("reproduce_cmd") or ()) or _DEFAULT_TEST_COMMAND)[0]
    return (interpreter, "-m", "pytest", *related, "-x", "--tb=short")


# ---------------------------------------------------------------------------
# Immunize (Tester) — crash-entry only.
# ---------------------------------------------------------------------------
async def immunize_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Immunise the just-healed crash with a regression test (crash entry only).

    Prefers a **targeted** test from the Tester (it sees the fix diff + the real
    source).  On a green tree the test is *gated*: it must PASS on the fixed tree
    or it is rolled back and we fall back to a deterministic **smoke** test
    (re-run the reproduce command, assert exit 0), which always passes on green.
    So immunization never commits a test that proves nothing, yet never silently
    disappears either.  On a *chained* error (tree still red) the targeted test
    is kept best-effort — a green re-run is impossible until the chain completes.

    Test-failure entry is skipped: a capturing test already exists.
    """
    if state.get("entry_kind", "crash") != "crash":
        return {"current_regression_test": None, "logs": ["immunize.skipped(test-entry)"]}

    workspace = state.get("workspace_path", "")
    test_path = state.get("regression_test_path", "")
    result = state.get("current_sandbox_result")
    tree_is_green = result is not None and result.verdict is SandboxVerdict.PASSED

    # 1) Preferred: a targeted test from the Tester (gated when the tree is green).
    try:
        draft = await deps.tester.write_regression_test(_build_fix_context(state))
        kept = await _gate_and_keep(
            deps, state, workspace, test_path, draft.source, gate=tree_is_green
        )
        if kept is not None:
            _LOGGER.info("node.immunize.done", extra={"node_id": kept.node_id, "kind": "targeted"})
            return {
                "current_regression_test": kept,
                "logs": [f"immunize.test={kept.node_id}(targeted)"],
            }
    except TestGenerationError as exc:
        _LOGGER.warning("node.immunize.tester_failed", extra={"error": str(exc)})

    # 2) Fallback: deterministic smoke test — only meaningful on a green tree
    #    (the gate must be able to pass it).
    if tree_is_green:
        smoke = _build_smoke_test_source(tuple(state.get("reproduce_cmd") or ()))
        kept = await _gate_and_keep(deps, state, workspace, test_path, smoke, gate=True)
        if kept is not None:
            _LOGGER.info("node.immunize.done", extra={"node_id": kept.node_id, "kind": "smoke"})
            return {
                "current_regression_test": kept,
                "logs": [f"immunize.test={kept.node_id}(smoke)"],
            }

    _LOGGER.warning("node.immunize.no_test")
    return {"current_regression_test": None, "logs": ["immunize.no_test"]}


async def _gate_and_keep(
    deps: Dependencies,
    state: HealingState,
    workspace: str,
    test_path: str,
    source: str,
    *,
    gate: bool,
) -> RegressionTest | None:
    """Append *source* to the session file, gating it on a green run.

    When *gate* is set the test is executed in the sandbox and rolled back
    if it does not pass.  Returns the kept test, or ``None`` if rejected.
    """
    before = _read_session_text(workspace, test_path)
    node_id = _append_regression_test(workspace, test_path, source)
    if gate:
        image = state.get("sandbox_image", deps.settings.sandbox_image)
        gate_cmd = ("python", "-m", "pytest", node_id, "-x", "--tb=short")
        res = await deps.sandbox.run_tests(
            workspace_path=workspace, image=image, command=gate_cmd
        )
        if res.verdict is not SandboxVerdict.PASSED:
            _restore_session_text(workspace, test_path, before)
            _LOGGER.warning(
                "node.immunize.gate_failed",
                extra={"node_id": node_id, "verdict": res.verdict.value},
            )
            return None
    return RegressionTest(path=test_path, node_id=node_id, source=source)


# Interpreter tokens replaced by ``sys.executable`` when building the smoke test.
_PY_INTERPRETERS: Final[frozenset[str]] = frozenset({"python", "python3", "py"})


def _smoke_invocation(reproduce_cmd: tuple[str, ...]) -> str:
    """``subprocess.run`` argv literal for the smoke test (python → sys.executable)."""
    cmd = tuple(reproduce_cmd) or ("python", "main.py")
    rest = cmd[1:] if cmd[0] in _PY_INTERPRETERS else cmd
    args = ", ".join(repr(a) for a in rest)
    return f"[sys.executable, {args}]" if args else "[sys.executable]"


def _build_smoke_test_source(
    reproduce_cmd: tuple[str, ...], name: str = "test_reproduce_command_exits_zero"
) -> str:
    """Deterministic fallback test: re-run the reproduce command, assert exit 0."""
    literal = _smoke_invocation(reproduce_cmd)
    return (
        f"def {name}():\n"
        "    import subprocess\n"
        "    import sys\n"
        f"    result = subprocess.run({literal}, capture_output=True, text=True)\n"
        "    assert result.returncode == 0, result.stderr\n"
    )


# ---------------------------------------------------------------------------
# Report + commit (Reporter writes the message).
# ---------------------------------------------------------------------------
async def report_commit_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Commit the healed error (Reporter-authored message) and advance the chain."""
    workspace = state.get("workspace_path", "")
    patch = state.get("current_patch")
    incident_id = state.get("current_incident_id", "unknown")
    cur_sig = state.get("current_error_signature")
    regression = state.get("current_regression_test")
    prior = tuple(_describe_attempt_for_retry(a) for a in _current_cycle_attempts(state))

    try:
        message = await deps.reporter_agent.compose_commit_message(
            incident_id=incident_id,
            error_signature=str(cur_sig) if cur_sig else "unknown error",
            diff_text=patch.diff_text if patch is not None else "",
            prior_attempts=prior,
            test_path=regression.path if regression is not None else None,
        )
    except Exception as exc:
        _LOGGER.warning("node.report_commit.reporter_failed", extra={"error": str(exc)})
        message = _fallback_commit_message(incident_id, cur_sig)

    # NB: never use the key "message" in logging ``extra`` — it is a reserved
    # LogRecord attribute and raises KeyError at DEBUG level.
    _LOGGER.debug("node.report_commit.message", extra={"commit_message": message[:1000]})
    sha = await deps.git.commit(
        workspace, message, deps.settings.git_author_name, deps.settings.git_author_email
    )
    fallback_kind: ErrorKind = state.get("entry_kind", "crash")
    resolved = ResolvedError(
        signature=cur_sig if cur_sig is not None else ErrorSignature(kind=fallback_kind),
        commit_sha=sha,
        test_path=regression.path if regression is not None else None,
    )
    _LOGGER.info("node.report_commit.committed", extra={"sha": sha, "incident": incident_id})

    residual = _residual_signature(state)
    cycle_index = state.get("error_cycle_index", 0)
    budget = state.get("error_cycle_budget", 1)
    update: HealingState = {
        "resolved_errors": [resolved],
        "is_resolved": True,
        "logs": [f"report_commit.sha={sha} resolved={resolved.signature}"],
    }
    if residual is not None and (cycle_index + 1) < budget:
        # Chained error: commit recorded progress; reset the per-error scratch
        # and re-enter the Corrector on the new error.
        update.update({
            "should_continue": True,
            "current_error_signature": residual,
            "current_failure_output": state.get("post_fix_output", ""),
            "current_incident_id": f"{incident_id}-chain{cycle_index + 1}",
            "error_cycle_index": cycle_index + 1,
            "attempt_count": 0,
            "current_patch": None,
            "current_sandbox_result": None,
            "current_regression_test": None,
            "post_fix_signature": None,
            "post_fix_output": "",
        })
        _LOGGER.info("node.report_commit.chain_advance", extra={"next_cycle": cycle_index + 1})
    else:
        update["should_continue"] = False
    return update


# ---------------------------------------------------------------------------
# Rollback (retry the same error).
# ---------------------------------------------------------------------------
async def rollback_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Revert the working tree and record a :class:`FailedAttempt`."""
    workspace = state.get("workspace_path", "")
    if workspace:
        try:
            await deps.git.reset_hard(workspace)
        except Exception:
            _LOGGER.exception("node.rollback.reset_failed", extra={"workspace": workspace})

    attempt_index = state.get("attempt_count", 0)
    record = FailedAttempt(
        attempt_index=attempt_index,
        cycle_index=state.get("error_cycle_index", 0),
        patch=state.get("current_patch"),
        sandbox_result=state.get("current_sandbox_result"),
        error_summary=_summarise_failure(state),
    )
    _LOGGER.warning(
        "node.rollback.recorded",
        extra={"attempt": attempt_index, "reason": record.error_summary},
    )
    # Feed the freshest failure text to the next Corrector run.
    freshest = state.get("post_fix_output") or state.get("current_failure_output", "")
    return {
        "failed_attempts": [record],
        "attempt_count": attempt_index + 1,
        "current_failure_output": freshest,
        "current_patch": None,
        "current_sandbox_result": None,
        "post_fix_signature": None,
        "post_fix_output": "",
        "regression_detected": False,
        "logs": [f"rollback.attempt_{attempt_index}: {record.error_summary}"],
    }


# ---------------------------------------------------------------------------
# Post-Mortem (Reporter narrative + filesystem persistence).
# ---------------------------------------------------------------------------
async def post_mortem_node(state: HealingState, deps: Dependencies) -> HealingState:
    """Persist the full attempt history as a Markdown report."""
    trigger = state["trigger"]
    incident_id = state.get("current_incident_id") or _incident_from_trigger(trigger)
    attempts = tuple(state.get("failed_attempts", []))
    resolved = tuple(state.get("resolved_errors", []))
    _LOGGER.info(
        "node.post_mortem.start",
        extra={"incident": incident_id, "attempts": len(attempts), "resolved": len(resolved)},
    )
    try:
        narrative = await deps.reporter_agent.compose_post_mortem(
            incident_id=incident_id, trigger=trigger, attempts=attempts, resolved=resolved
        )
    except Exception as exc:
        _LOGGER.warning("node.post_mortem.reporter_failed", extra={"error": str(exc)})
        narrative = ""
    path = await deps.reporter.write_post_mortem(
        incident_id, trigger, attempts, narrative=narrative
    )
    _LOGGER.info("node.post_mortem.done", extra={"path": path})
    return {
        "post_mortem_path": path,
        "is_resolved": False,
        "logs": [f"post_mortem.report={path}"],
    }


# ===========================================================================
# Helpers (pure / I/O-bounded).
# ===========================================================================


def _build_fix_context(state: HealingState) -> FixContext:
    """Assemble the uniform Corrector/Tester input from the state."""
    failing_test = state.get("failing_test")
    entry_kind = state.get("entry_kind", "crash")
    reproducer_node_id = (
        failing_test.node_id if (entry_kind == "test" and failing_test is not None) else None
    )
    prior = tuple(_describe_attempt_for_retry(a) for a in _current_cycle_attempts(state))
    patch = state.get("current_patch")
    return FixContext(
        incident_id=state.get("current_incident_id", "unknown"),
        failure_output=state.get("current_failure_output", ""),
        reproduce_cmd=tuple(state.get("reproduce_cmd") or ()),
        reproducer_node_id=reproducer_node_id,
        previous_attempts=prior,
        fix_diff=patch.diff_text if patch is not None else "",
    )


def _current_cycle_attempts(state: HealingState) -> list[FailedAttempt]:
    """Failed attempts belonging to the error currently being healed."""
    cycle = state.get("error_cycle_index", 0)
    return [a for a in state.get("failed_attempts", []) if a.cycle_index == cycle]


def _describe_attempt_for_retry(attempt: FailedAttempt) -> str:
    """Per-attempt context the Corrector sees on retry: the prior pytest tail."""
    lines = [attempt.error_summary]
    sandbox = attempt.sandbox_result
    if sandbox is not None and sandbox.logs_tail.strip():
        lines.append("Output (tail):")
        lines.append(sandbox.logs_tail[-_RETRY_TAIL_BUDGET:])
    return "\n".join(lines)


def _summarise_failure(state: HealingState) -> str:
    """Short human-readable reason for the current failed attempt."""
    if state.get("current_patch") is None:
        return "corrector produced no fix"
    if state.get("regression_detected"):
        return "fix cleared the target test but broke previously-passing tests (regression)"
    sandbox_result = state.get("current_sandbox_result")
    if sandbox_result is not None and sandbox_result.verdict is not SandboxVerdict.PASSED:
        return (
            f"sandbox verdict={sandbox_result.verdict.value} "
            f"(exit_code={sandbox_result.exit_code})"
        )
    return "unknown failure"


def _residual_signature(state: HealingState) -> ErrorSignature | None:
    """Return the chained error that remains after the current one was healed.

    ``None`` when the reproduction went fully green or the residual error is
    the *same* one we just (claimed to have) fixed.
    """
    result = state.get("current_sandbox_result")
    if result is not None and result.verdict is SandboxVerdict.PASSED:
        return None
    post = state.get("post_fix_signature")
    cur = state.get("current_error_signature")
    if post is not None and not post.matches(cur):
        return post
    return None


def _fallback_commit_message(incident_id: str, signature: ErrorSignature | None) -> str:
    """Deterministic commit message when the Reporter agent is unavailable."""
    sig = str(signature) if signature is not None else "unknown error"
    return (
        f"fix(self-healing): auto-patch for incident {incident_id}\n\n"
        f"Resolved error: {sig}\n"
    )


def _incident_from_trigger(trigger: TriggerEvent) -> str:
    if trigger.crash_report is not None:
        return trigger.crash_report.incident_id
    if trigger.failing_test is not None:
        return trigger.failing_test.node_id
    return "unknown"


def _ensure_session_test_file(workspace: str, rel_path: str, session_id: str) -> None:
    """Create the session regression file once (header only), best-effort."""
    if not workspace:
        return
    try:
        path = Path(workspace) / rel_path
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'"""Self-healing regression suite for session {session_id}.\n\n'
            "Auto-generated; one test is appended per healed error.\n"
            '"""\n',
            encoding="utf-8",
        )
    except OSError as exc:
        _LOGGER.warning(
            "session_test_file.create_failed", extra={"path": rel_path, "error": str(exc)}
        )


def _read_session_text(workspace: str, rel_path: str) -> str | None:
    """Snapshot the session file content (``None`` if absent) for gate rollback."""
    if not workspace or not rel_path:
        return None
    try:
        path = Path(workspace) / rel_path
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


def _restore_session_text(workspace: str, rel_path: str, content: str | None) -> None:
    """Roll the session file back to *content* — drops a gate-failed test."""
    if not workspace or not rel_path:
        return
    try:
        path = Path(workspace) / rel_path
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _LOGGER.warning(
            "session_test_file.restore_failed", extra={"path": rel_path, "error": str(exc)}
        )


def _append_regression_test(workspace: str, rel_path: str, source: str) -> str:
    """Append *source* to the session file and return the new test's node id."""
    func = _first_test_func(source)
    node_id = f"{rel_path}::{func}"
    if not workspace:
        return node_id
    try:
        path = Path(workspace) / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        sep = "" if existing.endswith("\n\n") or not existing else "\n\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{sep}{source.rstrip()}\n")
    except OSError as exc:
        _LOGGER.warning(
            "regression_test.append_failed", extra={"path": rel_path, "error": str(exc)}
        )
    return node_id


def _first_test_func(source: str) -> str:
    """Return the first ``test_*`` function name (``test_regression`` on failure)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "test_regression"
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("test_"):
            return node.name
    return "test_regression"


__all__ = [
    "bootstrap_node",
    "fix_node",
    "immunize_node",
    "post_mortem_node",
    "report_commit_node",
    "rollback_node",
    "validate_node",
]
