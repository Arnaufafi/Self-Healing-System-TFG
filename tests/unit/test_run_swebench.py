"""Unit tests for the SWE-bench runner's pure helpers (no Docker, no dataset)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from run_swebench import (  # noqa: E402
    extract_model_patch,
    image_for,
    model_patch_diff_args,
    parse_test_paths,
    write_prediction,
)

_TEST_PATCH = (
    "diff --git a/tests/test_x.py b/tests/test_x.py\n"
    "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/tests/test_y.py b/tests/test_y.py\n"
    "--- a/tests/test_y.py\n+++ b/tests/test_y.py\n@@ -1 +1 @@\n-a\n+b\n"
)


def test_parse_test_paths() -> None:
    assert parse_test_paths(_TEST_PATCH) == ["tests/test_x.py", "tests/test_y.py"]
    assert parse_test_paths("") == []


def test_model_patch_diff_args_excludes_test_paths() -> None:
    args = model_patch_diff_args("BASESHA", ["tests/test_x.py"])
    assert args[:4] == ["diff", "BASESHA", "HEAD", "--"]
    assert ":(exclude)tests/test_x.py" in args


def test_image_for() -> None:
    assert image_for("psf__requests-1", "sweb.eval.x86_64.{instance_id}:latest") == \
        "sweb.eval.x86_64.psf__requests-1:latest"


def test_write_prediction_appends_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "preds.jsonl"
    write_prediction(p, "inst-1", "DIFF-A")
    write_prediction(p, "inst-2", "DIFF-B")
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    assert [r["instance_id"] for r in rows] == ["inst-1", "inst-2"]
    assert rows[0]["model_name_or_path"] == "selfheal"
    assert rows[0]["model_patch"] == "DIFF-A"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_extract_model_patch_is_source_only(tmp_path: Path) -> None:
    """model_patch = base..HEAD minus the test files (the fix, not the test patch)."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_it():\n    assert False\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()

    # Commit T: the "test patch" modifies the test file.
    (tmp_path / "test_app.py").write_text("def test_it():\n    assert fixed()\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "test patch")

    # The agent's fix: edits source, commits.
    (tmp_path / "src.py").write_text("x = 2  # fixed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fix")

    patch = extract_model_patch(tmp_path, base, ["test_app.py"])
    assert "src.py" in patch and "x = 2" in patch  # the fix is in
    assert "test_app.py" not in patch              # the test patch is out
