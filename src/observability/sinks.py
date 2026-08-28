"""Telemetry sinks (adapters for :class:`~src.core.ports.telemetry.TelemetryPort`).

* :class:`InMemoryTelemetry` — keeps spans in a list and aggregates them; ideal
  for the benchmark summary and tests.
* :class:`JsonlTelemetry` — appends one JSON object per span to a file.
* :class:`MultiTelemetry` — fans each span out to several sinks.

:class:`~src.core.domain.NullTelemetry` (the no-op default) lives in the domain
layer and is re-exported here for convenience.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from src.core.domain import NullTelemetry, Span
from src.core.ports import TelemetryPort

_LOGGER = logging.getLogger(__name__)

__all__ = ["InMemoryTelemetry", "JsonlTelemetry", "MultiTelemetry", "NullTelemetry"]


def _llm_bucket() -> dict[str, float]:
    """Create a fresh tokens/cost accumulator for the ``llm`` aggregate."""
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
    }


class InMemoryTelemetry:
    """Collects spans in memory and aggregates them on demand. Thread-safe."""

    def __init__(self) -> None:
        """Start with an empty, lock-protected span list."""
        self._spans: list[Span] = []
        self._lock = threading.Lock()

    def record(self, span: Span) -> None:
        """See :meth:`TelemetryPort.record`."""
        with self._lock:
            self._spans.append(span)

    @property
    def spans(self) -> list[Span]:
        """Snapshot of the spans recorded so far."""
        with self._lock:
            return list(self._spans)

    def aggregate(self) -> dict[str, object]:
        """Per-span-name roll-up + a tokens/cost breakdown (total and per agent).

        ``llm.completion`` spans (emitted by the litellm callback) carry token
        counts and cost; here they are summed into ``llm.total`` and
        ``llm.by_agent`` so a run reports overall spend *and* which agent spent
        it (corrector / tester / reporter).
        """
        by_name: dict[str, dict[str, float]] = {}
        total = 0.0
        llm_total = _llm_bucket()
        llm_by_agent: dict[str, dict[str, float]] = {}
        for s in self.spans:
            total += s.duration_s
            bucket = by_name.setdefault(
                s.name, {"count": 0, "errors": 0, "total_s": 0.0, "max_s": 0.0}
            )
            bucket["count"] += 1
            bucket["errors"] += 1 if s.status == "error" else 0
            bucket["total_s"] += s.duration_s
            bucket["max_s"] = max(bucket["max_s"], s.duration_s)
            if s.name == "llm.completion":
                agent = str(s.attributes.get("agent", "unknown"))
                per_agent = llm_by_agent.setdefault(agent, _llm_bucket())
                for tgt in (llm_total, per_agent):
                    tgt["calls"] += 1
                    tgt["prompt_tokens"] += int(s.attributes.get("prompt_tokens", 0) or 0)
                    tgt["cached_tokens"] += int(s.attributes.get("cached_tokens", 0) or 0)
                    tgt["completion_tokens"] += int(s.attributes.get("completion_tokens", 0) or 0)
                    tgt["cost_usd"] += float(s.attributes.get("cost_usd", 0.0) or 0.0)
        for bucket in by_name.values():
            count = bucket["count"]
            bucket["avg_s"] = round(bucket["total_s"] / count, 4) if count else 0.0
            bucket["total_s"] = round(bucket["total_s"], 4)
            bucket["max_s"] = round(bucket["max_s"], 4)
        for bucket in (llm_total, *llm_by_agent.values()):
            bucket["cost_usd"] = round(bucket["cost_usd"], 6)
        # Ordered list of node names the run walked through (the path through the
        # graph), for tracing / plotting: e.g. ["bootstrap", "fix", "validate",
        # "immunize", "report_commit"]. Port and llm spans are excluded.
        node_path = [
            s.name.split(".", 1)[1] for s in self.spans if s.name.startswith("node.")
        ]
        return {
            "span_count": len(self.spans),
            "total_duration_s": round(total, 4),
            "node_path": node_path,
            "by_name": by_name,
            "llm": {"total": llm_total, "by_agent": llm_by_agent},
        }


class JsonlTelemetry:
    """Appends each span as a JSON line. Best-effort: never raises to the caller."""

    def __init__(self, path: str | Path) -> None:
        """Point the sink at *path*, creating parent directories best-effort."""
        self._path = Path(path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - fs guard
            _LOGGER.warning("telemetry.jsonl.mkdir_failed", extra={"error": str(exc)})

    def record(self, span: Span) -> None:
        """See :meth:`TelemetryPort.record`."""
        try:
            line = json.dumps(
                {
                    "name": span.name,
                    "duration_s": round(span.duration_s, 6),
                    "status": span.status,
                    "error_type": span.error_type,
                    "timestamp": span.timestamp,
                    "attributes": span.attributes,
                },
                default=str,
            )
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            _LOGGER.debug("telemetry.jsonl.write_failed", exc_info=True)


class MultiTelemetry:
    """Fan-out sink: forwards every span to each child sink (best-effort)."""

    def __init__(self, *sinks: TelemetryPort) -> None:
        """Store the child *sinks*."""
        self._sinks = sinks

    def record(self, span: Span) -> None:
        """Forward *span* to every child sink, swallowing their errors."""
        for sink in self._sinks:
            try:
                sink.record(span)
            except Exception:
                pass
