"""Unit tests for the LLMTester response parsing and the targeted prompt.

The provider call itself is not exercised (it needs litellm + a key); these
pin the pure logic — turning a model response into a usable test function, and
the prompt that drives a *targeted* (not generic) regression test.
"""

from __future__ import annotations

from src.agents.llm_tester import _SYSTEM_PROMPT, LLMTester, _extract_test_source
from src.core.domain import FixContext


def test_extracts_plain_test_function() -> None:
    raw = "def test_average_is_mean():\n    from m import average\n    assert average([2]) == 2\n"
    src = _extract_test_source(raw)
    assert src is not None and src.startswith("def test_average_is_mean():")


def test_extracts_from_markdown_fence() -> None:
    raw = "Here is the test:\n```python\ndef test_x():\n    assert True\n```\nDone."
    src = _extract_test_source(raw)
    assert src is not None
    assert src.strip() == "def test_x():\n    assert True"


def test_strips_leading_prose_before_def() -> None:
    raw = "I'll write this:\n\ndef test_y():\n    assert 1 == 1\n"
    src = _extract_test_source(raw)
    assert src is not None and src.startswith("def test_y():")


def test_returns_none_without_a_test_function() -> None:
    assert _extract_test_source("sorry, I cannot help with that") is None
    assert _extract_test_source("") is None


# ---------------------------------------------------------------------------
# Targeted-test prompt: the Tester is given the failure, the fix diff and the
# real source so it writes a test that attacks the specific bug with the real
# API — not a generic smoke test (that is the immunize node's fallback).
# ---------------------------------------------------------------------------


def test_system_prompt_drives_a_targeted_test() -> None:
    low = _SYSTEM_PROMPT.lower()
    assert "targeted" in low
    assert "exact names" in low  # must use the real API from the source
    assert "do not invent" in low
    assert "pytest.raises" in _SYSTEM_PROMPT  # still forbidden


def test_system_prompt_follows_the_picco_structure() -> None:
    for section in ("## Persona", "## Intention", "## Context", "## Conditions", "## Output"):
        assert section in _SYSTEM_PROMPT, f"missing PICCO section: {section}"


def test_user_prompt_includes_failure_diff_and_reproduce() -> None:
    ctx = FixContext(
        incident_id="inc-9",
        failure_output="AttributeError: 'GestorBaseDatos' object has no attribute 'cargar_dato'",
        reproduce_cmd=("python", "main.py"),
        fix_diff="--- a/g.py\n+++ b/g.py\n-  self.db.cargar_dato()\n+  self.db.cargar_datos()\n",
    )
    prompt = LLMTester(workspace_path=".")._render_user(ctx)
    assert "inc-9" in prompt
    assert "cargar_dato" in prompt  # the failure
    assert "Diff of the fix" in prompt  # the diff section is rendered
    assert "cargar_datos()" in prompt  # diff content reaches the model
    assert "python main.py" in prompt  # the reproduce command


def test_user_prompt_omits_diff_section_when_absent() -> None:
    ctx = FixContext(
        incident_id="i", failure_output="boom", reproduce_cmd=("python", "main.py")
    )
    prompt = LLMTester(workspace_path=".")._render_user(ctx)
    assert "Diff of the fix" not in prompt
