"""Unit tests for the test-entry helpers of the GitHub deploy entry point.

``heal_and_pr.py`` is a script (sibling-imports ``run_benchmark``), so we put
``scripts/`` on the path the same way the script itself does.  The two helpers
under test turn a failing pytest node into a ``TEST_FAILURE`` trigger.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from heal_and_pr import (  # noqa: E402
    build_test_trigger,
    detect_any_failure,
    detect_failing_test,
    shield_runtime_artifacts,
)

from src.core.domain import TriggerType  # noqa: E402

# ---------------------------------------------------------------------------
# build_test_trigger (pure)
# ---------------------------------------------------------------------------


def test_build_test_trigger_wraps_failing_test() -> None:
    trigger = build_test_trigger(
        "tests/test_calc.py::test_divide",
        source="def test_divide(): ...",
        output="E   ZeroDivisionError: division by zero",
    )
    assert trigger.trigger_type is TriggerType.TEST_FAILURE
    assert trigger.crash_report is None
    assert trigger.failing_test is not None
    assert trigger.failing_test.node_id == "tests/test_calc.py::test_divide"
    assert trigger.failing_test.source == "def test_divide(): ..."
    assert "ZeroDivisionError" in trigger.failing_test.last_failure_output


def test_build_test_trigger_truncates_long_output() -> None:
    trigger = build_test_trigger("t.py::t", source="x", output="A" * 9000)
    assert trigger.failing_test is not None
    assert len(trigger.failing_test.last_failure_output) == 4096  # last 4 KB only


# ---------------------------------------------------------------------------
# detect_failing_test (real pytest subprocess on a throw-away file)
# ---------------------------------------------------------------------------


def test_detect_failing_test_captures_source_and_output(tmp_path: Path) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_fails():\n    assert 1 == 2\n", encoding="utf-8")

    source, output = detect_failing_test(tmp_path, "test_sample.py::test_fails")

    assert "assert 1 == 2" in source           # the real test source, verbatim
    assert "test_fails" in output              # the captured pytest failure tail


def test_detect_failing_test_raises_when_node_passes(tmp_path: Path) -> None:
    test_file = tmp_path / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="passed"):
        detect_failing_test(tmp_path, "test_ok.py::test_ok")


def test_detect_failing_test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        detect_failing_test(tmp_path, "tests/does_not_exist.py::test_x")


# ---------------------------------------------------------------------------
# detect_any_failure (the on-push auto trigger: tests first, then crash)
# ---------------------------------------------------------------------------


def _write(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


def test_auto_picks_the_failing_test(tmp_path: Path) -> None:
    _write(tmp_path / "test_app.py", "def test_broken():\n    assert 1 == 2\n")

    detected = detect_any_failure(tmp_path)

    assert detected is not None
    trigger, reproduce_cmd = detected
    assert trigger.trigger_type is TriggerType.TEST_FAILURE
    assert trigger.failing_test is not None
    assert trigger.failing_test.node_id == "test_app.py::test_broken"
    assert "test_app.py::test_broken" in reproduce_cmd  # sandbox re-runs THAT node


def test_auto_falls_back_to_the_crash(tmp_path: Path) -> None:
    _write(tmp_path / "test_app.py", "def test_ok():\n    assert True\n")
    _write(tmp_path / "main.py", "raise RuntimeError('boom')\n")

    detected = detect_any_failure(tmp_path)

    assert detected is not None
    trigger, reproduce_cmd = detected
    assert trigger.trigger_type is TriggerType.PRODUCTION_CRASH
    assert trigger.crash_report is not None
    assert "boom" in trigger.crash_report.stack_trace
    assert reproduce_cmd == ()  # bootstrap derives `python main.py`


def test_auto_prefers_tests_over_the_crash(tmp_path: Path) -> None:
    _write(tmp_path / "test_app.py", "def test_broken():\n    assert 1 == 2\n")
    _write(tmp_path / "main.py", "raise RuntimeError('boom')\n")

    detected = detect_any_failure(tmp_path)

    assert detected is not None
    assert detected[0].trigger_type is TriggerType.TEST_FAILURE


def test_auto_returns_none_when_everything_is_green(tmp_path: Path) -> None:
    _write(tmp_path / "test_app.py", "def test_ok():\n    assert True\n")
    _write(tmp_path / "main.py", "print('all good')\n")

    assert detect_any_failure(tmp_path) is None


def test_auto_returns_none_on_an_empty_repo(tmp_path: Path) -> None:
    assert detect_any_failure(tmp_path) is None  # no tests, no main.py


# ---------------------------------------------------------------------------
# shield_runtime_artifacts (D16: probe-dirtied tracked files stay out of PRs)
# ---------------------------------------------------------------------------


def _seed_repo(tmp_path: Path) -> Path:
    """A tiny real git repo with a tracked runtime DB and a source file."""
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    _write(tmp_path / "db.json", '{"seed": true}\n')
    _write(tmp_path / "app.py", "x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    return tmp_path


def test_shield_restores_the_seed_and_keeps_the_db_out_of_the_commit(tmp_path: Path) -> None:
    import subprocess

    repo = _seed_repo(tmp_path)
    _write(repo / "db.json", '{"probe": "dirty"}\n')      # the probe ran the app

    shielded = shield_runtime_artifacts(repo)

    assert shielded == ["db.json"]
    assert (repo / "db.json").read_text(encoding="utf-8") == '{"seed": true}\n'

    # Later executions (Corrector / validation) re-dirty it; the fix edits app.py.
    _write(repo / "db.json", '{"validate": "dirty"}\n')
    _write(repo / "app.py", "x = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "heal"], cwd=repo, check=True, capture_output=True
    )

    show = subprocess.run(
        ["git", "show", "--stat", "--format="], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "app.py" in show       # the fix is committed
    assert "db.json" not in show  # the runtime artifact is not


def test_shield_is_a_noop_on_a_clean_tree(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    assert shield_runtime_artifacts(repo) == []
