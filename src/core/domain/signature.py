"""Error fingerprinting for chained-error detection.

The orchestrator must answer one question after every fix attempt: *is
this the same error I was trying to fix, or a different one?*  That single
bit drives the whole control flow:

* **same error still failing**  → the fix did not work → rollback + retry.
* **a different error surfaced** → the original is resolved → commit the
  progress and continue healing the new one (a *chained* error).
* **no error at all**           → fully green → commit and finish.

An :class:`ErrorSignature` is a stable fingerprint of a failure derived
from raw terminal text (a CPython traceback) or from pytest output.  It is
deliberately tolerant: volatile noise (absolute paths, hex addresses, line
numbers, temp dirs) is normalised away so the *same* underlying bug yields
the *same* fingerprint across runs, while a genuinely different exception
type / location / message yields a different one.

This module is pure (no I/O): callers pass the captured text in.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePath
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorKind = Literal["crash", "test"]


# --- Regexes -----------------------------------------------------------------
# CPython traceback frame:  File "path/to/file.py", line 42, in func
# The ``, in func`` tail is optional so SyntaxError / IndentationError frames
# (which omit it) still yield a location.
_TRACE_FRAME_RE: Final = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
# pytest --tb=short frame:  path/to/file.py:12: in test_foo
_PYTEST_FRAME_RE: Final = re.compile(r"^(?P<path>\S+\.py):(?P<line>\d+): in (?P<func>\S+)", re.M)
# pytest short summary:     FAILED path/to/test.py::test_foo - TypeError: boom
_PYTEST_SUMMARY_RE: Final = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<nodeid>\S+::\S+)"
    r"(?:\s+-\s+(?P<exc>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?)?",
    re.M,
)
# pytest assertion line:    E   TypeError: unsupported operand ...
_PYTEST_ERROR_LINE_RE: Final = re.compile(
    r"^E\s+(?P<exc>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?$", re.M
)
# A bare `path::node` id anywhere in the text.
_NODEID_RE: Final = re.compile(r"(?P<nodeid>\S+\.py::\S+)")
# Final traceback line:     TypeError: unsupported operand type(s) ...
_EXC_LINE_RE: Final = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*)(?::\s*(?P<msg>.*))?$")

# Volatile fragments scrubbed from messages so the fingerprint is stable.
_HEX_ADDR_RE: Final = re.compile(r"0x[0-9A-Fa-f]+")
_LINE_NO_RE: Final = re.compile(r"line \d+", re.I)
_WS_RE: Final = re.compile(r"\s+")

# Identifier suffixes that mark a final traceback line as an exception type.
_EXC_SUFFIXES: Final = ("Error", "Exception", "Exit", "Interrupt", "Warning", "Iteration")


class ErrorSignature(BaseModel):
    """Stable fingerprint of a single failure.

    Two signatures are considered the *same error* when their
    :attr:`fingerprint` matches.  The fingerprint folds in the exception
    type, the innermost in-workspace location and a normalised message —
    enough to tell chained errors apart without being so strict that line
    jitter looks like a brand-new bug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ErrorKind = Field(..., description="crash (program) or test (pytest) channel.")
    exc_type: str = Field(default="", description="Exception class name (empty if unknown).")
    location: str = Field(default="", description="Innermost workspace frame or pytest node id.")
    normalized_msg: str = Field(default="", description="Message with volatile noise scrubbed out.")

    @property
    def fingerprint(self) -> str:
        """Return a stable 16-char hash of the identifying fields.

        Deliberately keyed on ``(kind, exc_type, location)`` and **not** the
        message: the same broken spot often reports a slightly different
        message across runs (host vs sandbox traceback, line shifts, a
        parser that re-words the next indentation error).  Folding the
        message in made those look like brand-new *chained* errors, so a
        half-fix got committed and the pipeline "advanced" instead of
        retrying the same spot.  Type + location is the robust signal that
        the previous error is genuinely gone.
        """
        raw = f"{self.kind}|{self.exc_type}|{self.location}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def matches(self, other: ErrorSignature | None) -> bool:
        """Return ``True`` when *other* is the same underlying error."""
        return other is not None and self.fingerprint == other.fingerprint

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        """Render a one-line ``type @ location [fingerprint]`` summary."""
        loc = f" @ {self.location}" if self.location else ""
        return f"{self.exc_type or 'UnknownError'}{loc} [{self.fingerprint}]"


# --- Parsers -----------------------------------------------------------------


def from_crash_text(text: str, workspace_root: str = "") -> ErrorSignature | None:
    """Derive a signature from raw program crash output (a traceback).

    Returns ``None`` when *text* carries no usable failure information
    (empty / whitespace).  Best-effort: missing fields stay empty.
    """
    if not text or not text.strip():
        return None
    exc_type, message = _parse_exception_line(text)
    location = _innermost_workspace_frame(text, workspace_root)
    # A failure we cannot characterise at all (no exception type AND no frame)
    # is not a usable fingerprint — e.g. a sandbox infra message rather than a
    # real traceback. Returning None lets the router treat it as "same error /
    # cannot tell" (retry) instead of inventing a spurious chained error.
    if not exc_type and not location:
        return None
    return ErrorSignature(
        kind="crash",
        exc_type=exc_type,
        location=location,
        normalized_msg=_normalize_message(message, workspace_root),
    )


def from_pytest_output(text: str, workspace_root: str = "") -> ErrorSignature | None:
    """Derive a signature from pytest output.

    Returns ``None`` when the run looks green (``passed`` and no
    ``failed`` / ``error``) or when *text* is empty.
    """
    if not text or not text.strip():
        return None
    if re.search(r"\bpassed\b", text) and not re.search(r"\b(failed|error)\b", text, re.I):
        return None

    exc_type = ""
    message = ""
    location = ""

    summary = _PYTEST_SUMMARY_RE.search(text)
    if summary is not None:
        location = summary.group("nodeid") or ""
        exc_type = summary.group("exc") or ""
        message = summary.group("msg") or ""

    # The `E   ExcType: msg` line carries the full, untruncated exception class
    # name; the short-summary line is prone to terminal-width truncation (no
    # TTY => 80 cols, e.g. "FileNotFoundError" -> "F..."), so prefer the E-line
    # for the exception type whenever it is present.
    err_lines = list(_PYTEST_ERROR_LINE_RE.finditer(text))
    if err_lines:
        exc_type = err_lines[-1].group("exc") or exc_type
        message = err_lines[-1].group("msg") or message

    if not location:
        node = _NODEID_RE.search(text)
        if node is not None:
            location = node.group("nodeid")
        else:
            frame = _PYTEST_FRAME_RE.search(text)
            if frame is not None:
                location = f"{_to_rel(frame.group('path'), workspace_root)}:{frame.group('func')}"

    if not (exc_type or location):
        return None
    return ErrorSignature(
        kind="test",
        exc_type=exc_type,
        location=location,
        normalized_msg=_normalize_message(message, workspace_root),
    )


def parse_error(text: str, kind: ErrorKind, workspace_root: str = "") -> ErrorSignature | None:
    """Dispatch to the crash/test parser based on *kind*."""
    if kind == "crash":
        return from_crash_text(text, workspace_root)
    return from_pytest_output(text, workspace_root)


# --- Internals ---------------------------------------------------------------


def _parse_exception_line(text: str) -> tuple[str, str]:
    """Return ``(exc_type, message)`` from the last traceback line.

    A standard CPython traceback ends with ``ExcType: message`` (or a bare
    ``ExcType``).  We scan bottom-up for the first line that parses as an
    exception, skipping blank lines and trailing program chatter.
    """
    for line in reversed(text.strip().splitlines()):
        candidate = line.strip()
        if not candidate or candidate.startswith(("File \"", "Traceback", "    ", "\t")):
            continue
        m = _EXC_LINE_RE.match(candidate)
        if m is None:
            continue
        exc = m.group("exc")
        # Heuristic gate: only treat dotted/Error-ish identifiers as an
        # exception type, so a stray word line is not mistaken for one.
        if "." in exc or exc.endswith(_EXC_SUFFIXES):
            return exc, (m.group("msg") or "").strip()
    return "", text.strip().splitlines()[-1].strip() if text.strip() else ""


def _innermost_workspace_frame(text: str, workspace_root: str) -> str:
    """Return ``rel/path.py:func`` of the deepest in-workspace traceback frame.

    Falls back to the deepest frame overall (by basename) when no frame
    resolves inside the workspace — better a coarse-but-stable location
    than none.
    """
    frames = _TRACE_FRAME_RE.findall(text)
    if not frames:
        return ""
    root = _resolved(workspace_root) if workspace_root else None
    in_ws: list[str] = []
    for path, _line, func in frames:
        if root is not None and _is_inside(path, root):
            in_ws.append(_frame_label(_to_rel(path, workspace_root), func))
    if in_ws:
        return in_ws[-1]  # innermost in-workspace frame
    # No workspace frame: use the deepest frame's basename:func.
    last_path, _last_line, last_func = frames[-1]
    return _frame_label(PurePath(last_path).name, last_func)


def _frame_label(rel: str, func: str) -> str:
    """``rel:func`` when a function name is present, else just ``rel``."""
    return f"{rel}:{func}" if func else rel


def _normalize_message(message: str, workspace_root: str) -> str:
    """Scrub volatile noise so the same bug yields the same fingerprint."""
    if not message:
        return ""
    out = message
    if workspace_root:
        out = out.replace(workspace_root, "").replace(workspace_root.replace("\\", "/"), "")
    out = _HEX_ADDR_RE.sub("0xADDR", out)
    out = _LINE_NO_RE.sub("line N", out)
    out = _WS_RE.sub(" ", out).strip()
    return out.lower()


def _resolved(path: str) -> PurePath:
    return PurePath(path.replace("\\", "/"))


def _is_inside(path: str, root: PurePath) -> bool:
    try:
        return _resolved(path).is_relative_to(root)
    except (ValueError, OSError):
        return False


def _to_rel(path: str, workspace_root: str) -> str:
    """Workspace-relative POSIX path when possible, else the basename."""
    p = _resolved(path)
    if workspace_root:
        try:
            return p.relative_to(_resolved(workspace_root)).as_posix()
        except ValueError:
            pass
    return p.name


__all__ = ["ErrorKind", "ErrorSignature", "from_crash_text", "from_pytest_output", "parse_error"]
