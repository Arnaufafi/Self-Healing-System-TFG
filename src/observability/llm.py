"""litellm token/cost telemetry — captured once, for ALL agents, via a callback.

The port decorators cannot see *inside* an LLM call, so token/cost telemetry is
hooked at litellm itself: a single ``CustomLogger`` fires on every completion
(the Corrector's mini-swe-agent calls included) and records an
``llm.completion`` span.  Two context variables make it attributable without
touching business code:

* ``_current_agent`` — set by the agent decorators (``use_agent``) so each
  completion is tagged ``corrector`` / ``tester`` / ``reporter``.
* ``_current_sink`` — set around a run (``using_llm_sink``) so spans land in the
  same per-run sink as the node/port spans.

Both propagate into the worker thread mini-swe-agent uses (``asyncio.to_thread``
copies the context), so the Corrector's internal calls are captured too.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from src.core.domain import Span
from src.core.ports import TelemetryPort

_LOGGER = logging.getLogger(__name__)

_current_agent: ContextVar[str] = ContextVar("current_agent", default="unknown")
_current_sink: ContextVar[TelemetryPort | None] = ContextVar("llm_sink", default=None)

_logger_instance: Any = None


@contextlib.contextmanager
def use_agent(name: str) -> Iterator[None]:
    """Tag every litellm completion made inside the block with *name* (the agent)."""
    token = _current_agent.set(name)
    try:
        yield
    finally:
        _current_agent.reset(token)


@contextlib.contextmanager
def using_llm_sink(sink: TelemetryPort) -> Iterator[None]:
    """Route ``llm.completion`` spans made inside the block to *sink*."""
    token = _current_sink.set(sink)
    try:
        yield
    finally:
        _current_sink.reset(token)


def _env_float(name: str) -> float:
    """Read an environment variable as a float (``0.0`` when unset or invalid)."""
    raw = os.getenv(name)
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        _LOGGER.warning("llm_telemetry.bad_cost_rate", extra={"var": name, "value": raw})
        return 0.0


def _estimate_cost_from_tokens(
    prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0
) -> float:
    """Best-effort USD cost when litellm cannot price the model.

    litellm tariffs by the *model name*, so an Azure deployment with a custom
    name (or any model absent from its price table) yields a cost of ``0.0``.
    This fallback prices the captured token counts with the per-million-token
    rates in the environment:

    * ``CDD_LLM_INPUT_COST_PER_M``        — prompt tokens billed at full price.
    * ``CDD_LLM_CACHED_INPUT_COST_PER_M`` — prompt tokens served from the
      provider's prompt cache (cheaper); defaults to the full input rate.
    * ``CDD_LLM_OUTPUT_COST_PER_M``       — output tokens (``completion_tokens``
      already includes any reasoning tokens per the OpenAI/Azure spec).

    All rates default to ``0.0`` (disabled), so the estimate stays ``0.0`` —
    unchanged behaviour — unless they are set.
    """
    in_rate = _env_float("CDD_LLM_INPUT_COST_PER_M")
    cached_rate = _env_float("CDD_LLM_CACHED_INPUT_COST_PER_M")
    out_rate = _env_float("CDD_LLM_OUTPUT_COST_PER_M")
    if not (in_rate or cached_rate or out_rate):
        return 0.0
    uncached = max(prompt_tokens - cached_tokens, 0)
    cached_price = cached_rate if cached_rate else in_rate
    return (
        uncached / 1_000_000 * in_rate
        + cached_tokens / 1_000_000 * cached_price
        + completion_tokens / 1_000_000 * out_rate
    )


def _cached_tokens(usage: Any) -> int:
    """Provider-reported cached input tokens (``0`` when absent).

    OpenAI / Azure expose prompt-cache hits under
    ``usage.prompt_tokens_details.cached_tokens`` and bill them at the cheaper
    cached-input rate. Tolerates both object and dict shapes.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    if isinstance(details, dict):
        return int(details.get("cached_tokens", 0) or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def _record(kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
    """Emit one ``llm.completion`` span (tokens + cost) to the active sink."""
    sink = _current_sink.get()
    if sink is None:
        return
    try:
        usage = getattr(response_obj, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = _cached_tokens(usage)
        cost = kwargs.get("response_cost")
        if cost is None:
            try:
                import litellm

                cost = litellm.completion_cost(completion_response=response_obj)
            except Exception:
                cost = 0.0
        if not cost:
            # litellm has no price for this model (typically an Azure deployment
            # whose custom name is absent from its table): estimate it from the
            # token counts and the per-million rates in the environment.
            cost = _estimate_cost_from_tokens(prompt, completion, cached)
        try:
            duration = (end_time - start_time).total_seconds()
        except Exception:
            duration = 0.0
        sink.record(
            Span(
                name="llm.completion",
                duration_s=float(duration or 0.0),
                status="ok",
                attributes={
                    "agent": _current_agent.get(),
                    "model": kwargs.get("model") or getattr(response_obj, "model", ""),
                    "prompt_tokens": prompt,
                    "cached_tokens": cached,
                    "completion_tokens": completion,
                    "cost_usd": float(cost or 0.0),
                },
            )
        )
    except Exception:
        _LOGGER.debug("llm_telemetry.record_failed", exc_info=True)


def ensure_registered() -> None:
    """Register the litellm success callback (idempotent, best-effort)."""
    global _logger_instance
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger

        if _logger_instance is None:

            class _LLMTelemetry(CustomLogger):  # type: ignore[misc]
                def log_success_event(self, kwargs, response_obj, start_time, end_time):
                    _record(kwargs, response_obj, start_time, end_time)

                async def async_log_success_event(
                    self, kwargs, response_obj, start_time, end_time
                ):
                    _record(kwargs, response_obj, start_time, end_time)

            _logger_instance = _LLMTelemetry()

        callbacks = list(getattr(litellm, "callbacks", []) or [])
        if _logger_instance not in callbacks:
            litellm.callbacks = [*callbacks, _logger_instance]
    except Exception:
        _LOGGER.debug("llm_telemetry.register_failed", exc_info=True)
