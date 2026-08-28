"""Tiny shared litellm helper for the narrative agents (Tester, Reporter).

Keeps the provider call in one place so :class:`LLMTester` and
:class:`LLMReporter` stay focused on prompt construction.  litellm is
imported lazily so the package remains importable without it installed.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def acomplete(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout_seconds: float,
) -> str:
    """Run one chat completion and return the assistant text.

    Raises:
        RuntimeError: If litellm is missing, the call times out, or the
            provider returns an unexpected shape. Callers decide whether to
            surface or swallow this (the Reporter swallows; the Tester
            converts it to a domain error).
    """
    try:
        import litellm  # deferred: only needed in real mode
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("litellm is not installed. Install with: pip install litellm") from exc

    try:
        response: Any = await asyncio.wait_for(
            # ``drop_params=True`` lets litellm silently drop parameters the
            # target model does not accept (e.g. reasoning / *codex* models
            # reject ``temperature`` != 1) instead of raising — so the same
            # call works across OpenAI, Azure and local models unchanged.
            litellm.acompletion(
                model=model,
                messages=messages,
                temperature=temperature,
                drop_params=True,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise RuntimeError(f"LLM completion timed out after {timeout_seconds}s") from exc
    except Exception as exc:  # normalise any provider error into one type
        raise RuntimeError(f"LLM completion failed: {exc!s}") from exc

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        raise RuntimeError(f"LLM returned an unexpected response shape: {response!r}") from exc
    return content or ""
