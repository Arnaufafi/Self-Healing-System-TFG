"""Unit tests for the SWE-bench instance selector (no dataset needed)."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from swebench_select import (  # noqa: E402
    cost_proxy,
    parse_fail_to_pass,
    patch_n_files,
    patch_n_lines,
    select,
)

_ONE_FILE = "diff --git a/m.py b/m.py\n@@ -1 +1 @@\n-bad\n+good\n"
_TWO_FILE = _ONE_FILE + "diff --git a/n.py b/n.py\n@@ -1 +1 @@\n-x\n+y\n"


def test_parse_fail_to_pass_accepts_list_json_and_repr() -> None:
    assert parse_fail_to_pass(["a::b"]) == ["a::b"]
    assert parse_fail_to_pass('["a::b", "c::d"]') == ["a::b", "c::d"]
    assert parse_fail_to_pass("['a::b']") == ["a::b"]  # python repr (single quotes)
    assert parse_fail_to_pass("") == []
    assert parse_fail_to_pass(None) == []


def test_patch_counters() -> None:
    assert patch_n_files(_ONE_FILE) == 1
    assert patch_n_files(_TWO_FILE) == 2
    assert patch_n_lines(_ONE_FILE) == 4


def test_cost_proxy_penalises_heavy_repos() -> None:
    light = {"patch": _ONE_FILE, "problem_statement": "x", "repo": "acme/lib"}
    heavy = {"patch": _ONE_FILE, "problem_statement": "x", "repo": "django/django"}
    assert cost_proxy(heavy) > cost_proxy(light) + 50


def test_select_keeps_single_file_short_and_cheapest_first() -> None:
    big = "diff --git a/m.py b/m.py\n" + "\n".join(f"+l{i}" for i in range(40))
    instances = [
        {"instance_id": "two", "repo": "acme/lib", "patch": _TWO_FILE,
         "problem_statement": "s", "FAIL_TO_PASS": ["t::a"]},      # 2 files → out
        {"instance_id": "big", "repo": "acme/lib", "patch": big,
         "problem_statement": "s", "FAIL_TO_PASS": ["t::a"]},      # >30 lines → out
        {"instance_id": "noftp", "repo": "acme/lib", "patch": _ONE_FILE,
         "problem_statement": "s", "FAIL_TO_PASS": []},            # no FAIL_TO_PASS → out
        {"instance_id": "good", "repo": "acme/lib", "patch": _ONE_FILE,
         "problem_statement": "s", "FAIL_TO_PASS": ["t::a"]},      # kept
    ]
    chosen = select(instances, limit=8)
    assert [x["instance_id"] for x in chosen] == ["good"]


def test_select_excludes_pytest_and_requests() -> None:
    """pytest (self-referential build) and requests (network) are hard-skipped."""
    instances = [
        {"instance_id": "py", "repo": "pytest-dev/pytest", "patch": _ONE_FILE,
         "problem_statement": "s", "FAIL_TO_PASS": ["t::a"]},
        {"instance_id": "req", "repo": "psf/requests", "patch": _ONE_FILE,
         "problem_statement": "s", "FAIL_TO_PASS": ["t::a"]},
        {"instance_id": "keep", "repo": "pylint-dev/pylint", "patch": _ONE_FILE,
         "problem_statement": "s", "FAIL_TO_PASS": ["t::a"]},
    ]
    chosen = select(instances, limit=8)
    assert [x["instance_id"] for x in chosen] == ["keep"]
