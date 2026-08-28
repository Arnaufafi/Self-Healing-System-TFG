"""Unit tests for the decorator-based telemetry (observability layer).

Pins the contract that matters for a cross-cutting concern: spans are emitted
around every port call, the wrapped business logic is delegated verbatim,
exceptions are recorded **and re-raised**, and telemetry can never break the
flow (a failing sink is swallowed).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.domain import (
    FixContext,
    NullTelemetry,
    Patch,
    SandboxResult,
    SandboxVerdict,
    Span,
)
from src.observability import (
    InMemoryTelemetry,
    InstrumentedFixer,
    InstrumentedSandbox,
    instrument_node,
    span,
)
from src.observability.llm import _record, use_agent, using_llm_sink


class _ListSink:
    """Minimal TelemetryPort that records spans into a list."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def record(self, s: Span) -> None:
        self.spans.append(s)


# ---------------------------------------------------------------------------
# span()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_span_records_success_and_yields_mutable_attrs() -> None:
    sink = _ListSink()
    async with span(sink, "thing.do", foo="bar") as attrs:
        attrs["enriched"] = 1
    assert len(sink.spans) == 1
    s = sink.spans[0]
    assert s.name == "thing.do" and s.status == "ok" and s.error_type is None
    assert s.attributes == {"foo": "bar", "enriched": 1}
    assert s.duration_s >= 0.0


@pytest.mark.asyncio
async def test_span_records_error_and_reraises() -> None:
    sink = _ListSink()
    with pytest.raises(ValueError):
        async with span(sink, "thing.boom"):
            raise ValueError("boom")
    assert sink.spans[0].status == "error"
    assert sink.spans[0].error_type == "ValueError"


@pytest.mark.asyncio
async def test_span_never_breaks_flow_when_sink_raises() -> None:
    class _BadSink:
        def record(self, s: Span) -> None:
            raise RuntimeError("sink down")

    # The failing sink must be swallowed — the block completes normally.
    async with span(_BadSink(), "thing.do"):
        pass


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------


def test_in_memory_aggregate_rolls_up_by_name() -> None:
    tel = InMemoryTelemetry()
    tel.record(Span("a", 0.10, "ok"))
    tel.record(Span("a", 0.30, "error", error_type="X"))
    tel.record(Span("b", 0.05, "ok"))
    agg = tel.aggregate()
    assert agg["span_count"] == 3
    a = agg["by_name"]["a"]
    assert a["count"] == 2 and a["errors"] == 1
    assert a["avg_s"] == pytest.approx(0.2, abs=1e-3)
    assert a["max_s"] == pytest.approx(0.3, abs=1e-3)


def test_null_telemetry_is_noop() -> None:
    NullTelemetry().record(Span("x", 0.0))  # no error, no state


# ---------------------------------------------------------------------------
# decorators
# ---------------------------------------------------------------------------


class _FakeFixer:
    def __init__(self, patch: Patch | None) -> None:
        self._patch = patch

    async def fix(self, context: FixContext) -> Patch | None:
        return self._patch


def _ctx() -> FixContext:
    return FixContext(
        incident_id="inc-1", failure_output="boom", reproduce_cmd=("python", "main.py")
    )


@pytest.mark.asyncio
async def test_instrumented_fixer_delegates_and_tags_patch() -> None:
    sink = _ListSink()
    patch = Patch(diff_text="--- a\n+++ b\n", author_agent="x")
    out = await InstrumentedFixer(_FakeFixer(patch), sink).fix(_ctx())
    assert out is patch
    s = sink.spans[0]
    assert s.name == "fixer.fix"
    assert s.attributes["incident_id"] == "inc-1"
    assert s.attributes["produced_patch"] is True


@pytest.mark.asyncio
async def test_instrumented_fixer_records_none_patch() -> None:
    sink = _ListSink()
    out = await InstrumentedFixer(_FakeFixer(None), sink).fix(_ctx())
    assert out is None
    assert sink.spans[0].attributes["produced_patch"] is False


@pytest.mark.asyncio
async def test_instrumented_fixer_records_then_propagates_error() -> None:
    class _Boom:
        async def fix(self, context: FixContext) -> Patch | None:
            raise RuntimeError("agent crashed")

    sink = _ListSink()
    with pytest.raises(RuntimeError):
        await InstrumentedFixer(_Boom(), sink).fix(_ctx())
    assert sink.spans[0].status == "error"
    assert sink.spans[0].error_type == "RuntimeError"


class _FakeSandbox:
    async def run_tests(
        self, workspace_path: str, image: str, command: tuple[str, ...]
    ) -> SandboxResult:
        return SandboxResult(
            verdict=SandboxVerdict.PASSED, duration_seconds=0.0, logs_tail=""
        )


@pytest.mark.asyncio
async def test_instrumented_sandbox_tags_verdict_and_command() -> None:
    sink = _ListSink()
    res = await InstrumentedSandbox(_FakeSandbox(), sink).run_tests(
        "/ws", "img", ("python", "main.py")
    )
    assert res.verdict is SandboxVerdict.PASSED
    s = sink.spans[0]
    assert s.name == "sandbox.run_tests"
    assert s.attributes["verdict"] == SandboxVerdict.PASSED.value
    assert s.attributes["command"] == "python main.py"


# ---------------------------------------------------------------------------
# instrument_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instrument_node_records_span_and_forwards_args() -> None:
    sink = _ListSink()

    async def _node(state: dict) -> dict:
        return {"seen": state}

    wrapped = instrument_node("fix", _node, sink)
    out = await wrapped({"x": 1})
    assert out == {"seen": {"x": 1}}
    assert sink.spans[0].name == "node.fix"


# ---------------------------------------------------------------------------
# LLM token/cost telemetry (litellm callback → llm.completion span)
# ---------------------------------------------------------------------------


def _fake_response(prompt: int, completion: int, model: str = "gpt-4.1-mini"):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        model=model,
    )


def test_llm_record_emits_completion_span_tagged_by_agent() -> None:
    sink = _ListSink()
    t0 = datetime.now()
    t1 = t0 + timedelta(seconds=1.5)
    with using_llm_sink(sink), use_agent("tester"):
        _record({"model": "gpt-4.1-mini", "response_cost": 0.0003}, _fake_response(100, 20), t0, t1)
    assert len(sink.spans) == 1
    s = sink.spans[0]
    assert s.name == "llm.completion"
    assert s.attributes["agent"] == "tester"
    assert s.attributes["prompt_tokens"] == 100
    assert s.attributes["completion_tokens"] == 20
    assert s.attributes["cost_usd"] == 0.0003
    assert s.duration_s == pytest.approx(1.5, abs=1e-2)


def test_llm_record_estimates_cost_from_token_rates_when_litellm_has_no_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An Azure deployment with a custom name has no litellm price, so the call
    # arrives with no ``response_cost``; the env rates estimate it from tokens.
    monkeypatch.setenv("CDD_LLM_INPUT_COST_PER_M", "1.0")  # $1 per 1M input tokens
    monkeypatch.setenv("CDD_LLM_OUTPUT_COST_PER_M", "2.0")  # $2 per 1M output tokens
    sink = _ListSink()
    t0 = datetime.now()
    with using_llm_sink(sink):
        _record({"model": "openai/custom-deploy"}, _fake_response(1000, 500, "openai/x"), t0, t0)
    # 1000/1e6 * 1.0 + 500/1e6 * 2.0 = 0.001 + 0.001 = 0.002
    assert sink.spans[0].attributes["cost_usd"] == pytest.approx(0.002, abs=1e-9)


def test_llm_record_without_token_rates_keeps_cost_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the env rates the fallback is a no-op: cost stays 0.0 (unchanged).
    monkeypatch.delenv("CDD_LLM_INPUT_COST_PER_M", raising=False)
    monkeypatch.delenv("CDD_LLM_OUTPUT_COST_PER_M", raising=False)
    sink = _ListSink()
    t0 = datetime.now()
    with using_llm_sink(sink):
        _record({"model": "openai/custom-deploy"}, _fake_response(1000, 500, "openai/x"), t0, t0)
    assert sink.spans[0].attributes["cost_usd"] == 0.0


def test_llm_record_prices_cached_input_at_the_cheaper_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prompt caching: of 1000 prompt tokens, 800 were served from cache and must
    # be billed at the cheaper cached rate, not the full input rate.
    monkeypatch.setenv("CDD_LLM_INPUT_COST_PER_M", "1.75")
    monkeypatch.setenv("CDD_LLM_CACHED_INPUT_COST_PER_M", "0.175")
    monkeypatch.setenv("CDD_LLM_OUTPUT_COST_PER_M", "14.0")
    resp = _fake_response(1000, 100, "openai/x")
    resp.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=800)
    sink = _ListSink()
    t0 = datetime.now()
    with using_llm_sink(sink):
        _record({"model": "openai/custom-deploy"}, resp, t0, t0)
    # uncached 200*1.75 + cached 800*0.175 + output 100*14.0, per 1M tokens
    expected = (200 * 1.75 + 800 * 0.175 + 100 * 14.0) / 1_000_000
    span = sink.spans[0]
    assert span.attributes["cached_tokens"] == 800
    assert span.attributes["cost_usd"] == pytest.approx(expected, abs=1e-12)


def test_llm_record_is_a_noop_without_an_active_sink() -> None:
    # No `using_llm_sink` → the sink contextvar is None → records nothing, no error.
    _record({"model": "m"}, _fake_response(1, 1), datetime.now(), datetime.now())


def test_aggregate_rolls_up_llm_tokens_and_cost_total_and_per_agent() -> None:
    tel = InMemoryTelemetry()
    calls = [
        {"agent": "corrector", "prompt_tokens": 100, "cached_tokens": 80,
         "completion_tokens": 50, "cost_usd": 0.002},
        {"agent": "corrector", "prompt_tokens": 40, "completion_tokens": 10, "cost_usd": 0.001},
        {"agent": "tester", "prompt_tokens": 20, "completion_tokens": 5, "cost_usd": 0.0005},
    ]
    for duration, attrs in zip((1.0, 0.5, 0.3), calls, strict=True):
        tel.record(Span("llm.completion", duration, "ok", attrs))
    llm = tel.aggregate()["llm"]
    assert llm["total"]["calls"] == 3
    assert llm["total"]["prompt_tokens"] == 160
    assert llm["total"]["cached_tokens"] == 80
    assert llm["total"]["completion_tokens"] == 65
    assert llm["total"]["cost_usd"] == pytest.approx(0.0035, abs=1e-6)
    assert llm["by_agent"]["corrector"]["calls"] == 2
    assert llm["by_agent"]["corrector"]["prompt_tokens"] == 140
    assert llm["by_agent"]["corrector"]["cached_tokens"] == 80
    assert llm["by_agent"]["tester"]["calls"] == 1
    assert llm["by_agent"]["tester"]["cost_usd"] == pytest.approx(0.0005, abs=1e-6)


def test_aggregate_records_node_path_in_order() -> None:
    """node_path is the ordered list of graph nodes walked (port/llm spans aside),
    repeats included so a retry shows fix/validate twice — ready to plot."""
    tel = InMemoryTelemetry()
    for name in (
        "node.bootstrap", "node.fix", "fixer.fix", "node.validate",
        "node.rollback", "node.fix", "node.validate", "node.report_commit",
    ):
        tel.record(Span(name, 0.1, "ok", {}))
    assert tel.aggregate()["node_path"] == [
        "bootstrap", "fix", "validate", "rollback", "fix", "validate", "report_commit",
    ]
