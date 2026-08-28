"""Unit tests for the immunize node's deterministic smoke-test fallback.

When a targeted regression test does not pass the gate, the node falls back to
this guaranteed-on-green smoke test so immunization never silently disappears.
"""

from __future__ import annotations

import ast

from src.orchestrator.nodes import _build_smoke_test_source, _smoke_invocation


def test_smoke_invocation_uses_sys_executable() -> None:
    assert _smoke_invocation(("python", "main.py")) == "[sys.executable, 'main.py']"


def test_smoke_invocation_handles_python3_and_extra_args() -> None:
    assert _smoke_invocation(("python3", "-m", "app")) == "[sys.executable, '-m', 'app']"


def test_smoke_invocation_defaults_when_empty() -> None:
    assert _smoke_invocation(()) == "[sys.executable, 'main.py']"


def test_build_smoke_test_source_is_valid_python() -> None:
    src = _build_smoke_test_source(("python", "main.py"))
    assert src.startswith("def test_reproduce_command_exits_zero():")
    assert "subprocess.run([sys.executable, 'main.py']" in src
    assert "result.returncode == 0" in src
    ast.parse(src)  # must be syntactically valid
