"""Unit tests for related-test selection and validate's no-regression gate.

The general (non-SWE-bench) gate selects the tests that exercise the files a fix
touched — by import or by name — and requires them to stay green. The selection
helpers are tested directly; the gate is exercised through ``validate_node`` with
the in-memory sandbox scripting the reproduce/gate verdicts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents import MockFixer, MockReporterAgent, MockTester
from src.config import Settings
from src.core.domain import HealingState, Patch, SandboxVerdict
from src.infrastructure.docker_sandbox import InMemorySandbox
from src.infrastructure.git_ops import InMemoryGit
from src.infrastructure.persistence import FilesystemReporter
from src.orchestrator.dependencies import Dependencies
from src.orchestrator.nodes import validate_node
from src.orchestrator.regression import changed_source_files, select_related_tests

# --- changed_source_files ---------------------------------------------------


def test_changed_source_files_keeps_source_drops_tests() -> None:
    diff = (
        "diff --git a/pkg/math_utils.py b/pkg/math_utils.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/tests/test_math_utils.py b/tests/test_math_utils.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/README.md b/README.md\n@@ -1 +1 @@\n-d\n+e\n"
    )
    assert changed_source_files(diff) == ["pkg/math_utils.py"]


def test_changed_source_files_empty_diff() -> None:
    assert changed_source_files("") == []


# --- select_related_tests (import OR name) ----------------------------------


def test_select_related_by_import_and_name(tmp_path: Path) -> None:
    (tmp_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # related by IMPORT (name does not match the module)
    (tmp_path / "test_alpha.py").write_text(
        "from math_utils import add\n\ndef test_x():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    # related by NAME (imports nothing special)
    (tmp_path / "test_math_utils.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    # unrelated
    (tmp_path / "test_other.py").write_text(
        "import os\n\ndef test_z():\n    pass\n", encoding="utf-8"
    )

    related = select_related_tests(str(tmp_path), ["math_utils.py"])
    assert "test_alpha.py" in related          # by import
    assert "test_math_utils.py" in related     # by name
    assert "test_other.py" not in related


def test_select_related_handles_from_package_import(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "misc.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    # pylint-style: differently-named test that imports the module from its package
    (tmp_path / "tests" / "unittest_misc.py").write_text(
        "from pkg import misc\n\ndef test_v():\n    assert misc.VALUE == 1\n", encoding="utf-8"
    )
    related = select_related_tests(str(tmp_path), ["pkg/misc.py"])
    assert related == ["tests/unittest_misc.py"]


def test_select_related_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "lonely.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_other.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    assert select_related_tests(str(tmp_path), ["lonely.py"]) == []


# --- validate_node: the general gate over related tests ---------------------


def _deps(sandbox: InMemorySandbox, reports_dir: Path) -> Dependencies:
    """Bundle where only the sandbox + settings matter to ``validate_node``."""
    settings = Settings(log_json=False, reports_dir=reports_dir)
    return Dependencies(
        settings=settings,
        fixer=MockFixer(),
        tester=MockTester(),
        reporter_agent=MockReporterAgent(),
        sandbox=sandbox,
        git=InMemoryGit(),
        reporter=FilesystemReporter(settings),
    )


def _state_with_fix(workspace: Path, diff: str) -> HealingState:
    return {
        "workspace_path": str(workspace),
        "entry_kind": "test",
        "reproduce_cmd": ("python", "-m", "pytest", "x", "-x"),
        "current_patch": Patch(diff_text=diff, author_agent="m"),
        "current_error_signature": None,
        # no regression_cmd ⇒ general mode ⇒ validate selects related tests
    }


@pytest.mark.asyncio
async def test_validate_gates_on_related_tests_and_flags_break(tmp_path: Path) -> None:
    """Target green, but a related test fails ⇒ regression ⇒ retry."""
    (tmp_path / "math_utils.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_math_utils.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    diff = "diff --git a/math_utils.py b/math_utils.py\n@@ -1 +1 @@\n-x\n+y\n"
    # reproduce → PASSED; related-tests gate → FAILED
    deps = _deps(
        InMemorySandbox(scripted_verdicts=(SandboxVerdict.PASSED, SandboxVerdict.FAILED)),
        tmp_path / "reports",
    )
    out = await validate_node(_state_with_fix(tmp_path, diff), deps)
    assert out["regression_detected"] is True


@pytest.mark.asyncio
async def test_validate_no_related_tests_proceeds(tmp_path: Path) -> None:
    """No test relates to the touched file ⇒ gate disabled ⇒ proceed (green)."""
    (tmp_path / "lonely.py").write_text("x = 1\n", encoding="utf-8")
    diff = "diff --git a/lonely.py b/lonely.py\n@@ -1 +1 @@\n-x\n+y\n"
    # only the reproduce runs; the gate never fires (no related tests)
    deps = _deps(InMemorySandbox(scripted_verdicts=(SandboxVerdict.PASSED,)), tmp_path / "reports")
    out = await validate_node(_state_with_fix(tmp_path, diff), deps)
    assert out.get("regression_detected") is False
