"""litellm-backed Reporter implementing :class:`ReporterAgentPort`.

The Reporter is the only narrative agent: it never touches the workspace or
the sandbox.  It composes a Conventional-Commits message for each healed
error and a post-mortem narrative when the retry budget is exhausted.

Both methods are **best-effort and never raise**: a provider failure falls
back to a deterministic template (commit) or an empty narrative (post-mortem),
so a flaky LLM can never block a commit or lose a report.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Final

from src.agents._llm import acomplete
from src.core.domain import FailedAttempt, ResolvedError, TriggerEvent

_LOGGER = logging.getLogger(__name__)

# PICCO prompt structure: Persona, Intention, Context, Conditions, Output.
_COMMIT_SYSTEM: Final[str] = textwrap.dedent("""\
    ## Persona
    You are the Reporter: a release engineer who writes commit messages for
    automated bug fixes.

    ## Intention
    Record the healed error so a human reviewer instantly understands the root
    cause and the fix.

    ## Context
    The user message gives you the incident, the error signature, the diff of
    the fix, and the regression test added (when any).

    ## Conditions
    - Conventional Commits, type `fix`.
    - Summary line: imperative, <=72 chars.
    - Body: 1-3 short sentences on the root cause and the fix.

    ## Output
    ONLY the commit message, no markdown, no prose around it:
      <type>(<scope>): <imperative summary, <=72 chars>

      <body>
    """)

_POSTMORTEM_SYSTEM: Final[str] = textwrap.dedent("""\
    ## Persona
    You are the Reporter: the incident scribe of an automated self-healing
    pipeline.

    ## Intention
    Summarise a healing run that could NOT fix an error within its retry
    budget, so a human can pick the incident up quickly.

    ## Context
    The user message gives you the incident, the trigger, the errors healed
    earlier in the chain, and a summary of each failed attempt.

    ## Conditions
    - 3-5 sentences, Markdown.
    - State what failed, what was attempted, and a likely next step for a
      human.

    ## Output
    ONLY the summary prose.
    """)


class LLMReporter:
    """Production Reporter backed by litellm."""

    def __init__(
        self,
        *,
        model_name: str = "claude-sonnet-4-20250514",
        temperature: float = 0.2,
        timeout_seconds: float = 90.0,
    ) -> None:
        """Store the model configuration."""
        self._model_name = model_name
        self._temperature = temperature
        self._timeout = timeout_seconds

    async def compose_commit_message(
        self,
        *,
        incident_id: str,
        error_signature: str,
        diff_text: str,
        prior_attempts: tuple[str, ...],
        test_path: str | None,
    ) -> str:
        """See :meth:`ReporterAgentPort.compose_commit_message`."""
        user = "".join([
            f"Incident: {incident_id}\n",
            f"Error fixed: {error_signature}\n",
            f"Attempts before success: {len(prior_attempts)}\n",
            (f"Regression test added: {test_path}\n" if test_path else ""),
            "\n## Diff of the fix\n",
            f"```diff\n{diff_text[:6000]}\n```\n",
        ])
        try:
            message = await acomplete(
                model=self._model_name,
                messages=[
                    {"role": "system", "content": _COMMIT_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=self._temperature,
                timeout_seconds=self._timeout,
            )
        except RuntimeError as exc:
            _LOGGER.warning("llm_reporter.commit.fallback", extra={"error": str(exc)})
            return _fallback_commit(incident_id, error_signature)
        message = message.strip()
        return message or _fallback_commit(incident_id, error_signature)

    async def compose_post_mortem(
        self,
        *,
        incident_id: str,
        trigger: TriggerEvent,
        attempts: tuple[FailedAttempt, ...],
        resolved: tuple[ResolvedError, ...],
    ) -> str:
        """See :meth:`ReporterAgentPort.compose_post_mortem`."""
        summaries = "\n".join(f"- attempt {a.attempt_index}: {a.error_summary}" for a in attempts)
        user = "".join([
            f"Incident: {incident_id}\n",
            f"Trigger: {trigger.trigger_type.value}\n",
            f"Errors healed earlier in the chain: {len(resolved)}\n",
            f"Failed attempts on the final error: {len(attempts)}\n",
            f"\n## Attempt summaries\n{summaries}\n",
        ])
        try:
            narrative = await acomplete(
                model=self._model_name,
                messages=[
                    {"role": "system", "content": _POSTMORTEM_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=self._temperature,
                timeout_seconds=self._timeout,
            )
        except RuntimeError as exc:
            _LOGGER.warning("llm_reporter.post_mortem.fallback", extra={"error": str(exc)})
            return ""
        return narrative.strip()


def _fallback_commit(incident_id: str, error_signature: str) -> str:
    """Deterministic commit message used when the LLM call fails."""
    return (
        f"fix(self-healing): resolve {error_signature}\n\n"
        f"Auto-healed incident {incident_id} by the self-healing pipeline.\n"
    )


__all__ = ["LLMReporter"]
