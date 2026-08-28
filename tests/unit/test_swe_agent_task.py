"""Contract tests for the MiniSWEFixer task prompt and retry context.

The prompt and the retry helper together drive the Corrector's
edit-verify-iterate loop.  These tests pin the invariants that matter for
that loop (success command, submit gate, focus-file pre-loading, trajectory
tail) without being brittle to wording changes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch as mock_patch

from jinja2 import StrictUndefined, Template

from src.agents.swe_agent_dev import (
    _INSTANCE_TEMPLATE,
    _MODEL_KWARGS,
    _SYSTEM_TEMPLATE,
    MiniSWEFixer,
    _docker_run_args,
)
from src.core.domain import FailedAttempt, FixContext, SandboxResult, SandboxVerdict
from src.orchestrator.nodes import _describe_attempt_for_retry


def _make_agent(workspace: str = ".") -> MiniSWEFixer:
    """Construct an agent without running ``_verify_install`` so unit tests
    do not need mini-swe-agent installed."""
    with mock_patch.object(MiniSWEFixer, "_verify_install", staticmethod(lambda: None)):
        return MiniSWEFixer(workspace_path=workspace)


def _test_ctx(node_id: str = "tests/test_calc.py::test_indentation_error") -> FixContext:
    return FixContext(
        incident_id="inc-1",
        failure_output="E   IndentationError: unexpected indent\n",
        reproduce_cmd=("python", "-m", "pytest", node_id, "-x", "--tb=short"),
        reproducer_node_id=node_id,
    )


def _crash_ctx() -> FixContext:
    return FixContext(
        incident_id="inc-2",
        failure_output=(
            "Traceback (most recent call last):\n"
            '  File "main.py", line 5, in run\n'
            "TypeError: bad operand\n"
        ),
        reproduce_cmd=("python", "main.py"),
        reproducer_node_id=None,
    )


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------


def test_test_entry_success_criterion_is_the_pytest_node() -> None:
    task = _make_agent()._build_task(_test_ctx())
    assert "python -m pytest tests/test_calc.py::test_indentation_error" in task


def test_crash_entry_success_criterion_is_the_reproduce_command() -> None:
    task = _make_agent()._build_task(_crash_ctx())
    assert "`python main.py`" in task


def test_task_prompt_keeps_explicit_submit_gate() -> None:
    """Weak local models bail without an explicit "don't submit yet" rule."""
    task = _make_agent()._build_task(_test_ctx())
    assert "DO NOT submit" in task
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in task
    assert "exit with code 0" in task


def test_task_prompt_forbids_writing_tests() -> None:
    """The Corrector must never write tests — that is the Tester's job."""
    task = _make_agent()._build_task(_test_ctx())
    assert "do not write or modify any test files" in task.lower()


def test_task_prompt_includes_failure_output() -> None:
    task = _make_agent()._build_task(_test_ctx())
    assert "IndentationError: unexpected indent" in task


def test_task_prompt_renders_previous_attempts_when_present() -> None:
    ctx = _test_ctx().model_copy(
        update={"previous_attempts": ("first failure log", "second failure log")}
    )
    task = _make_agent()._build_task(ctx)
    assert "Previous failed attempts" in task
    assert "Attempt 1" in task and "first failure log" in task
    assert "Attempt 2" in task and "second failure log" in task


def test_task_prompt_omits_history_section_on_first_try() -> None:
    task = _make_agent()._build_task(_test_ctx())
    assert "Previous failed attempts" not in task


# ---------------------------------------------------------------------------
# Prompt templates: tool-calling native + safe edits
#
# mini-swe-agent's ``LitellmModel`` always runs in function-calling mode and
# rejects any turn without a ``bash`` tool call.  The stock ``default`` config
# instructs a *text block* instead, so a compliant model thrashes on
# "No tool calls found".  These tests pin that we drive the agent with a
# tool-calling-native prompt and force a tool call every turn.
# ---------------------------------------------------------------------------


def test_system_prompt_is_tool_calling_native() -> None:
    """Must tell the model to CALL the bash tool, not emit a text block."""
    assert "bash" in _SYSTEM_TEMPLATE.lower()
    # The stock text-block fence is what caused the format thrashing.
    assert "mswea_bash_command" not in _SYSTEM_TEMPLATE
    assert "mswea_bash_command" not in _INSTANCE_TEMPLATE


def test_model_kwargs_force_a_tool_call_every_turn() -> None:
    assert _MODEL_KWARGS.get("tool_choice") == "required"


def test_docker_run_args_mount_at_the_given_workdir() -> None:
    """The container bind + cwd follow the workdir (``/testbed`` for SWE-bench)."""
    args = _docker_run_args("/g/repo", "/testbed")
    assert "--rm" in args
    assert "-v" in args
    assert "/g/repo:/testbed" in args  # mounted at the workdir, not /workspace
    default = _docker_run_args("/g/repo", "/workspace")
    assert "/g/repo:/workspace" in default


def test_instance_prompt_steers_indentation_fixes_to_heredoc_not_sed() -> None:
    """sed space-counting is the classic way a model breaks a file anew."""
    lowered = _INSTANCE_TEMPLATE.lower()
    assert "indentationerror" in lowered
    assert "<<'eof'" in lowered  # recommends a quoted here-document
    # Warns against using sed for whitespace surgery.
    assert "do not add or remove spaces with `sed`" in lowered


def test_instance_prompt_forbids_dropping_unseen_code_on_rewrite() -> None:
    """Guard for the whole-file-rewrite-drops-code failure mode: the Corrector
    once dropped `dividir` while rewriting a file it had only partially viewed,
    spawning a chained AttributeError. The prompt must require seeing the whole
    file first and must forbid omitting code."""
    lowered = _INSTANCE_TEMPLATE.lower()
    assert "entire contents" in lowered          # step 1: read the whole file
    assert "never omit code you did not" in lowered  # don't drop unseen code


def test_templates_render_with_only_task_defined() -> None:
    """Guards against a future edit adding an undefined Jinja var: the agent
    renders with StrictUndefined, so anything but ``{{task}}`` crashes at run
    time inside the Docker thread where it is hardest to diagnose."""
    sys_out = Template(_SYSTEM_TEMPLATE, undefined=StrictUndefined).render(task="T")
    inst_out = Template(_INSTANCE_TEMPLATE, undefined=StrictUndefined).render(task="THE_BODY")
    assert "THE_BODY" in inst_out
    assert sys_out.strip()  # non-empty system prompt


def test_prompts_follow_the_picco_structure() -> None:
    """The conversation (system + instance) must cover the five PICCO sections:
    Persona + Intention live in the system template, Context + Conditions +
    Output in the instance template."""
    assert "## Persona" in _SYSTEM_TEMPLATE
    assert "## Intention" in _SYSTEM_TEMPLATE
    assert "## Context" in _INSTANCE_TEMPLATE
    assert "## Conditions" in _INSTANCE_TEMPLATE
    assert "## Output" in _INSTANCE_TEMPLATE


# ---------------------------------------------------------------------------
# Focus-file pre-loading
# ---------------------------------------------------------------------------


def test_focus_files_pre_loaded_from_module_not_found(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "gestor_cuentas.py").write_text("class CuentaBancaria:\n    pass\n")
    agent = _make_agent(workspace=str(tmp_path))
    ctx = FixContext(
        incident_id="inc",
        failure_output=(
            "tests/test_calc.py:6: in test_gestor_cuentas_import\n"
            "    from gestor_cuentas import CuentaBancaria\n"
            "E   ModuleNotFoundError: No module named 'gestor_cuentas'\n"
        ),
        reproduce_cmd=("python", "-m", "pytest", "-x"),
        reproducer_node_id="tests/test_calc.py::test_gestor_cuentas_import",
    )
    task = agent._build_task(ctx)
    assert "## Project context (read-only)" in task
    assert "### `gestor_cuentas.py`" in task
    assert "class CuentaBancaria" in task


def test_focus_files_skip_the_reproducer_test_file(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_x(): assert False  # MARKER\n")
    agent = _make_agent(workspace=str(tmp_path))
    ctx = FixContext(
        incident_id="inc",
        failure_output="tests/test_calc.py:1: AssertionError\n",
        reproduce_cmd=("python", "-m", "pytest", "-x"),
        reproducer_node_id="tests/test_calc.py::test_x",
    )
    task = agent._build_task(ctx)
    assert "## Project context (read-only)" not in task
    assert "MARKER" not in task


def test_focus_files_respect_budget_innermost_first(tmp_path: Path) -> None:
    """Budget caps the embedded files, keeping the innermost (deepest) frames."""
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (tmp_path / name).write_text(f"# content of {name}\n")
    agent = _make_agent(workspace=str(tmp_path))
    ctx = FixContext(
        incident_id="inc",
        failure_output=(
            'File "a.py", line 1\nFile "b.py", line 2\n'
            'File "c.py", line 3\nFile "d.py", line 4\n'
        ),
        reproduce_cmd=("python", "main.py"),
    )
    task = agent._build_task(ctx)
    # Budget 3, innermost-first: the 3 deepest frames (d, c, b) are embedded;
    # the outermost (a) is dropped.
    assert "# content of d.py" in task
    assert "# content of c.py" in task
    assert "# content of b.py" in task
    assert "# content of a.py" not in task


def test_focus_files_include_innermost_buggy_frame(tmp_path: Path) -> None:
    """Regression: on an import chain the deepest frame — where the bug lives —
    must be embedded, not dropped in favour of the importing modules."""
    for name in ("main.py", "gestor.py", "calc.py"):
        (tmp_path / name).write_text(f"# {name} content\n")
    agent = _make_agent(workspace=str(tmp_path))
    ctx = FixContext(
        incident_id="inc",
        failure_output=(
            'File "main.py", line 2, in <module>\n'
            '    from gestor import X\n'
            'File "gestor.py", line 2, in <module>\n'
            '    from calc import Y\n'
            'File "calc.py", line 21\n'
            "IndentationError: unindent does not match any outer indentation level\n"
        ),
        reproduce_cmd=("python", "main.py"),
    )
    task = agent._build_task(ctx)
    assert "# calc.py content" in task  # the buggy innermost file is present


# ---------------------------------------------------------------------------
# Trajectory tail
# ---------------------------------------------------------------------------


def test_trajectory_tail_preserves_last_turn_under_budget() -> None:
    msgs = [
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "y" * 5000},
        {"role": "assistant", "content": "FINAL THOUGHT"},
    ]
    tail = MiniSWEFixer._format_trajectory_tail(msgs)
    assert "FINAL THOUGHT" in tail
    assert len(tail) < 4_000


def test_trajectory_tail_renders_chronologically() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    tail = MiniSWEFixer._format_trajectory_tail(msgs)
    assert tail.index("first") < tail.index("second")


# ---------------------------------------------------------------------------
# Retry context (_describe_attempt_for_retry)
# ---------------------------------------------------------------------------


def test_retry_description_includes_output_tail() -> None:
    attempt = FailedAttempt(
        attempt_index=0,
        sandbox_result=SandboxResult(
            verdict=SandboxVerdict.FAILED,
            exit_code=1,
            duration_seconds=0.1,
            logs_tail="E   AssertionError: expected 42, got 7\n",
        ),
        error_summary="sandbox verdict=failed (exit_code=1)",
    )
    descr = _describe_attempt_for_retry(attempt)
    assert "AssertionError: expected 42, got 7" in descr
    assert descr.startswith("sandbox verdict=failed")


def test_retry_description_handles_missing_sandbox_result() -> None:
    attempt = FailedAttempt(attempt_index=0, sandbox_result=None, error_summary="no fix produced")
    assert _describe_attempt_for_retry(attempt) == "no fix produced"
