"""Shared focus-file selection: read the workspace sources named in a failure.

Both the Corrector (to know what to edit) and the Tester (to know the real API
to write a test against) need the current contents of the files a traceback
points at.  Kept here so the two agents share one implementation —
innermost-frame-first, ``ModuleNotFound`` aware, budget-capped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

# CPython traceback frame:  File "path/to/file.py", line N, ...
_TRACE_FILE_RE: Final = re.compile(r'File "([^"]+\.py)", line \d+')
# Pytest --tb=short:        path/to/file.py:LINE:
_PYTEST_PATH_RE: Final = re.compile(r"(?:^|\s)([\w./\\-]+\.py)(?=:\d+:)")
# ``ModuleNotFoundError: No module named 'X'`` — the strongest hint for which
# workspace file matters, even though no path literal appears in the trace.
_MODULE_NOT_FOUND_RE: Final = re.compile(
    r"(?:ModuleNotFound|Import)Error: No module named ['\"]([\w.]+)['\"]"
)

DEFAULT_BUDGET: Final[int] = 3
DEFAULT_FILE_BYTES: Final[int] = 8 * 1024

_DEFAULT_INTRO: Final[str] = (
    "The files below are the current contents of the workspace files\n"
    "named in the failure trace.  Use them as the source of truth for\n"
    "what exists right now; modify them where the fix belongs.\n\n"
)


def collect_focus_paths(
    workspace: str,
    failure_output: str,
    node_id: str | None = None,
    *,
    budget: int = DEFAULT_BUDGET,
) -> list[str]:
    """Workspace-relative source paths named in *failure_output*, innermost first.

    Frames are prioritised innermost-first (the deepest frame is where the
    exception was raised); a ``ModuleNotFound`` hint outranks everything.  The
    reproducer test file (``node_id``) is excluded so an agent never reads /
    edits it, and only paths that resolve to a real file inside the workspace
    are kept, capped at *budget*.
    """
    root = Path(workspace).resolve()
    test_abs = None
    if node_id:
        try:
            test_abs = (root / node_id.split("::", 1)[0]).resolve()
        except OSError:
            test_abs = None

    frames: list[str] = []
    for regex in (_TRACE_FILE_RE, _PYTEST_PATH_RE):
        frames.extend(m.group(1) for m in regex.finditer(failure_output))

    module_hints: list[str] = []
    for match in _MODULE_NOT_FOUND_RE.finditer(failure_output):
        module = match.group(1)
        dotted = module.replace(".", "/")
        module_hints.extend(
            [f"{dotted}.py", f"{dotted}/__init__.py", f"{module.split('.')[0]}.py"]
        )

    candidates = module_hints + list(reversed(frames))
    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        try:
            raw_path = Path(raw)
            abs_path = (
                raw_path.resolve()
                if raw_path.is_absolute()
                else (root / raw_path).resolve()
            )
            rel = abs_path.relative_to(root)
        except (OSError, ValueError):
            continue
        if test_abs is not None and abs_path == test_abs:
            continue
        if not abs_path.is_file():
            continue
        rel_str = rel.as_posix()
        if rel_str in seen:
            continue
        seen.add(rel_str)
        out.append(rel_str)
        if len(out) >= budget:
            break
    return out


def render_focus_block(
    workspace: str,
    failure_output: str,
    node_id: str | None = None,
    *,
    intro: str = _DEFAULT_INTRO,
    budget: int = DEFAULT_BUDGET,
    file_bytes: int = DEFAULT_FILE_BYTES,
) -> str:
    """Render the embedded source files as a Markdown block (``""`` when none)."""
    paths = collect_focus_paths(workspace, failure_output, node_id, budget=budget)
    if not paths:
        return ""
    out: list[str] = ["## Project context (read-only)\n", intro]
    for rel_path in paths:
        try:
            text = (Path(workspace) / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > file_bytes:
            text = text[:file_bytes] + "\n# ... (truncated)\n"
        out.append(f"### `{rel_path}`\n")
        out.append(f"```python\n{text}\n```\n\n")
    return "".join(out)
