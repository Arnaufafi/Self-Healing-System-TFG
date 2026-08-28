"""Structured logging bootstrap.

We deliberately stay on the standard library: a custom ``Formatter``
emits one JSON object per record, while developer-mode keeps the
familiar text format. Configure once via :func:`configure_logging`
during the application bootstrap; nodes obtain loggers with
``logging.getLogger(__name__)`` as usual.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

_RESERVED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render log records as compact one-line JSON objects.

    Any extra attribute attached to the record (via ``logger.info(..., extra={"key": "value"})``)
    is preserved verbatim under the same key in the output.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` as JSON."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Surface custom ``extra=`` fields without colliding with stdlib slots.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO", json_mode: bool = True) -> None:
    """Idempotently configure the root logger.

    Args:
        level: Log level name (e.g. ``"INFO"``).
        json_mode: When ``True`` emit JSON; otherwise human-readable text.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Remove pre-existing handlers to avoid duplicate emission when
    # ``configure_logging`` is called multiple times (e.g. in tests).
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    if json_mode:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
    root.addHandler(handler)
