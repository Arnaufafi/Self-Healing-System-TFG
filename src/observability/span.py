"""The timing primitive shared by every instrumentation decorator."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from src.core.domain import Span
from src.core.ports import TelemetryPort


@asynccontextmanager
async def span(
    sink: TelemetryPort, name: str, **attributes: Any
) -> AsyncIterator[dict[str, Any]]:
    """Time the wrapped block and emit one :class:`Span` to *sink*.

    Yields the (mutable) attributes dict so the caller can enrich the span with
    outcome data before it is recorded::

        async with span(sink, "sandbox.run_tests", command=cmd) as attrs:
            result = await inner.run_tests(...)
            attrs["verdict"] = result.verdict.value

    The span is always recorded (success or failure); on failure the exception
    is annotated and **re-raised** — the contract of the wrapped port is never
    altered.  Recording itself can never raise.
    """
    attrs: dict[str, Any] = dict(attributes)
    start = time.perf_counter()
    status = "ok"
    error_type: str | None = None
    try:
        yield attrs
    except BaseException as exc:
        status = "error"
        error_type = type(exc).__name__
        raise
    finally:
        try:
            sink.record(
                Span(
                    name=name,
                    duration_s=time.perf_counter() - start,
                    status=status,
                    attributes=attrs,
                    error_type=error_type,
                    timestamp=time.time(),
                )
            )
        except Exception:
            pass


def instrument_node(
    name: str, node: Callable[..., Awaitable[Any]], sink: TelemetryPort
) -> Callable[..., Awaitable[Any]]:
    """Wrap a LangGraph node coroutine so each invocation emits a span.

    The node is already bound to its :class:`Dependencies` (via
    ``functools.partial``), so the wrapper forwards whatever positional/keyword
    arguments LangGraph passes (the state, and optionally a runtime config).
    """

    async def _instrumented(*args: Any, **kwargs: Any) -> Any:
        async with span(sink, f"node.{name}"):
            return await node(*args, **kwargs)

    _instrumented.__name__ = f"instrumented_{name}"
    return _instrumented
