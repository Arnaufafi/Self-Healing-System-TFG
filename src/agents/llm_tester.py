"""litellm-backed Tester implementing :class:`TesterPort`.

Invoked *after* a green fix on the crash-entry path.  It makes a single LLM
call and **returns the test source** (it does not touch the workspace): the
``immunize`` node appends that source to the session regression file, runs it
as a gate, and — if it does not pass — rolls it back and falls back to a
deterministic smoke test (:func:`build_smoke_test_source`).

The prompt drives a **targeted** regression test: the model is given the
failure, the unified diff of the fix, and the *current* (fixed) source of the
files involved, and asked to call the specific function/behaviour that was
broken with inputs that exercise the bug, asserting the now-correct result.
Seeing the real source means it uses the real API (no hallucinated class
names); the anti-pattern rules still hold (imports inside the function, never
``pytest.raises`` on the fixed behaviour).
"""

from __future__ import annotations

import logging
import re
import textwrap
from typing import Final

from src.agents._focus import render_focus_block
from src.agents._llm import acomplete
from src.core.domain import FixContext, RegressionTest
from src.core.exceptions import TestGenerationError

_LOGGER = logging.getLogger(__name__)

# Intro for the source block — frames it as the *fixed* code to test against
# (the Corrector's intro frames it as code to edit, which would mislead here).
_TESTER_INTRO: Final[str] = (
    "The files below are the CURRENT (already-fixed) contents of the workspace\n"
    "files named in the failure.  Use them as the source of truth for the real\n"
    "API — import the classes/functions exactly as named here.\n\n"
)

# PICCO prompt structure: Persona, Intention, Context, Conditions, Output.
_SYSTEM_PROMPT: Final[str] = textwrap.dedent("""\
    ## Persona
    You are the Tester: a senior QA engineer who writes pytest regression tests
    for bugs that were JUST fixed.

    ## Intention
    Immunise the fix with ONE TARGETED test: call the specific function or
    method that was broken, with inputs that would have triggered the bug, and
    assert the now-correct behaviour — so it passes on the fixed code and would
    have FAILED before the fix.

    ## Context
    The user message gives you the failure that was fixed, the unified diff of
    the fix, and the current (already-fixed) source of the files involved — the
    source of truth for the real API.

    ## Conditions
    - Put every import INSIDE the function body — the function is appended to a
      shared test file, so module-level imports are unsafe.
    - Import the project's modules/classes using the EXACT names shown in the
      source (real class name, real module path). Do not invent names.
    - NEVER use `pytest.raises` on the fixed behaviour — assert the correct
      result/return value instead.
    - Exercise the exact behaviour the diff changed; keep the setup minimal.
    - Name the function after the behaviour it locks in.

    ## Output
    ONLY Python: exactly one `def test_<name>():` function. No prose, no
    markdown fences, no module-level code.
    """)


class LLMTester:
    """Production Tester backed by litellm."""

    def __init__(
        self,
        *,
        model_name: str = "claude-sonnet-4-20250514",
        workspace_path: str = ".",
        temperature: float = 0.0,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Store the model configuration and the workspace (read for sources)."""
        self._model_name = model_name
        self._workspace = workspace_path
        self._temperature = temperature
        self._timeout = timeout_seconds

    async def write_regression_test(self, context: FixContext) -> RegressionTest:
        """See :meth:`TesterPort.write_regression_test`."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": self._render_user(context)},
        ]
        _LOGGER.info(
            "llm_tester.write.start",
            extra={"incident_id": context.incident_id, "model": self._model_name},
        )
        try:
            raw = await acomplete(
                model=self._model_name,
                messages=messages,
                temperature=self._temperature,
                timeout_seconds=self._timeout,
            )
        except RuntimeError as exc:
            raise TestGenerationError(f"Tester LLM call failed: {exc!s}") from exc

        source = _extract_test_source(raw)
        if source is None:
            raise TestGenerationError(
                "Tester returned no usable `def test_*` function. Raw head:\n" + raw[:512]
            )
        _LOGGER.info("llm_tester.write.done", extra={"incident_id": context.incident_id})
        # path / node_id are advisory — the immunize node overrides them with
        # the session regression file and the real first-test-function name.
        return RegressionTest(path="", node_id="", source=source)

    def _render_user(self, context: FixContext) -> str:
        reproduce = " ".join(context.reproduce_cmd) if context.reproduce_cmd else "python main.py"
        parts = [
            f"Incident: {context.incident_id}\n\n",
            "A bug was just fixed. Write the targeted regression test that locks "
            "in the corrected behaviour.\n\n",
            "## Failure that was fixed\n",
            f"```\n{context.failure_output}\n```\n\n",
        ]
        if context.fix_diff.strip():
            parts.append("## Diff of the fix (what changed)\n")
            parts.append(f"```diff\n{context.fix_diff}\n```\n\n")
        focus = render_focus_block(
            self._workspace,
            context.failure_output,
            context.reproducer_node_id,
            intro=_TESTER_INTRO,
        )
        if focus:
            parts.append(focus)
        parts.append(
            f"## How the behaviour is exercised\nReproduction command: `{reproduce}`\n"
        )
        return "".join(parts)


# --- Parsing -----------------------------------------------------------------

_FENCE_RE: Final = re.compile(r"```(?:python)?\n(?P<body>.*?)\n```", re.DOTALL)
_DEF_TEST_RE: Final = re.compile(r"^def\s+test_\w*\s*\(", re.MULTILINE)


def _extract_test_source(raw: str) -> str | None:
    """Pull the first ``def test_*`` block out of a (possibly fenced) response.

    Returns ``None`` when no test function is present.
    """
    if not raw or not raw.strip():
        return None
    fence = _FENCE_RE.search(raw)
    body = fence.group("body") if fence else raw
    m = _DEF_TEST_RE.search(body)
    if m is None:
        return None
    source = body[m.start():].rstrip()
    return source + "\n" if source else None


__all__ = ["LLMTester"]
