"""Prompt-contract tests for the Reporter agent.

Pins the PICCO structure (Persona, Intention, Context, Conditions, Output)
and the conditions that keep its output machine-consumable: the commit
message must be a Conventional-Commits ``fix`` and nothing else, the
post-mortem prose and nothing else.
"""

from __future__ import annotations

import pytest

from src.agents.llm_reporter import _COMMIT_SYSTEM, _POSTMORTEM_SYSTEM

_PICCO_SECTIONS = ("## Persona", "## Intention", "## Context", "## Conditions", "## Output")


@pytest.mark.parametrize(
    "prompt", [_COMMIT_SYSTEM, _POSTMORTEM_SYSTEM], ids=["commit", "postmortem"]
)
def test_prompts_follow_the_picco_structure(prompt: str) -> None:
    for section in _PICCO_SECTIONS:
        assert section in prompt, f"missing PICCO section: {section}"


def test_commit_prompt_pins_conventional_commits_fix() -> None:
    assert "`fix`" in _COMMIT_SYSTEM
    assert "<=72 chars" in _COMMIT_SYSTEM
    assert "ONLY the commit message" in _COMMIT_SYSTEM


def test_postmortem_prompt_pins_prose_only_output() -> None:
    assert "ONLY the summary prose" in _POSTMORTEM_SYSTEM
    assert "next step" in _POSTMORTEM_SYSTEM
