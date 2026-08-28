"""Related-test selection for the no-regression gate.

Outside SWE-bench there is no curated ``PASS_TO_PASS`` list, so the gate builds
one from the fix itself: after a green fix, find the tests that exercise the
source files the fix touched and require them to keep passing. This protects the
developers' tests *for that file* without paying for the whole suite.

"Related" combines two signals:

* **import** — a test file that imports a changed module (by basename), so it
  works even when the test is not named after the file (e.g. pylint's
  ``unittest_misc.py`` for ``misc.py``).
* **name** — ``foo.py`` ↔ ``test_foo.py`` / ``foo_test.py``.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

_DIFF_GIT_RE: Final = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)", re.MULTILINE)
# Common test-file conventions: test_x.py, x_test.py, x_tests.py, unittest_x.py
# (pylint uses the last one), so import-related tests are found across repos.
_TEST_FILE_RE: Final = re.compile(r"(^|/)(test_[^/]+|[^/]+_tests?|unittest_[^/]+)\.py$")
# Directories never worth scanning for tests (and slow / huge on real repos).
_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {".git", "venv", ".venv", "node_modules", "__pycache__", ".tox", "build", "dist", ".mypy_cache"}
)


def changed_source_files(diff_text: str) -> list[str]:
    """Workspace-relative ``.py`` *source* files a unified diff touches (tests out)."""
    seen: dict[str, None] = {}
    for m in _DIFF_GIT_RE.finditer(diff_text):
        path = m.group("b")
        if path.endswith(".py") and not _TEST_FILE_RE.search(path):
            seen.setdefault(path, None)
    return list(seen)


def _is_test_file(path: Path) -> bool:
    return bool(_TEST_FILE_RE.search(path.as_posix()))


def _imported_basenames(source: str) -> set[str]:
    """Last component of every module a file imports (``import a.b.c`` → ``c``)."""
    out: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.rsplit(".", 1)[-1])
            for alias in node.names:  # ``from pkg import math`` → ``math``
                out.add(alias.name.rsplit(".", 1)[-1])
    return out


def select_related_tests(workspace: str, changed_files: list[str]) -> list[str]:
    """Workspace-relative test files related to *changed_files* (import OR name).

    A test file qualifies when it imports a changed module (by basename) or its
    name matches a changed file (``foo.py`` ↔ ``test_foo.py`` / ``foo_test.py``).
    Returns a sorted, de-duplicated list; empty when nothing matches.
    """
    stems = {Path(f).stem for f in changed_files}
    if not stems:
        return []
    name_targets = {f"test_{s}" for s in stems} | {f"{s}_test" for s in stems}
    root = Path(workspace)
    related: set[str] = set()
    for path in root.rglob("*.py"):
        if _SKIP_DIRS.intersection(path.parts) or not _is_test_file(path):
            continue
        rel = path.relative_to(root).as_posix()
        if path.stem in name_targets:  # by name
            related.add(rel)
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _imported_basenames(source) & stems:  # by import
            related.add(rel)
    return sorted(related)


__all__ = ["changed_source_files", "select_related_tests"]
