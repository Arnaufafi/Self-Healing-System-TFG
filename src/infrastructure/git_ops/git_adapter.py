"""Asynchronous Git adapter.

GitPython is synchronous and CPU-light but I/O-heavy, so we offload its
operations to threads via :func:`asyncio.to_thread`.  The adapter only
exposes the two operations the orchestrator needs under the
*apply-in-place* contract: ``commit`` (when an attempt succeeds) and
``reset_hard`` (to drop the agent's edits when an attempt fails).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.exceptions import GitOperationError
from src.core.ports import GitPort

_LOGGER = logging.getLogger(__name__)


class GitAdapter(GitPort):
    """GitPython-backed implementation of :class:`GitPort`."""

    def __init__(self) -> None:
        """Lazily resolve the GitPython dependency."""
        self._git_module: Any | None = None

    def _ensure_git(self) -> Any:
        """Import GitPython on demand to keep the core import-light."""
        if self._git_module is not None:
            return self._git_module
        try:
            import git  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise GitOperationError("GitPython is not installed.") from exc
        self._git_module = git
        return git

    # ------------------------------------------------------------------
    async def commit(
        self,
        workspace_path: str,
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """See :meth:`GitPort.commit`."""
        _LOGGER.info("git.commit.start", extra={"workspace": workspace_path})
        try:
            sha = await asyncio.to_thread(
                self._commit_blocking, workspace_path, message, author_name, author_email
            )
        except GitOperationError:
            _LOGGER.exception("git.commit.error", extra={"workspace": workspace_path})
            raise
        _LOGGER.info("git.commit.done", extra={"sha": sha})
        return sha

    def _commit_blocking(
        self,
        workspace_path: str,
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        git = self._ensure_git()
        try:
            repo = git.Repo(workspace_path)
            repo.git.add(A=True)
            author = git.Actor(author_name, author_email)
            commit = repo.index.commit(message, author=author, committer=author)
            return commit.hexsha
        except Exception as exc:
            raise GitOperationError(f"Commit failed: {exc!s}") from exc

    # ------------------------------------------------------------------
    async def reset_hard(self, workspace_path: str) -> None:
        """See :meth:`GitPort.reset_hard`."""
        _LOGGER.info("git.reset_hard.start", extra={"workspace": workspace_path})
        try:
            await asyncio.to_thread(self._reset_blocking, workspace_path)
        except GitOperationError:
            _LOGGER.exception("git.reset_hard.error", extra={"workspace": workspace_path})
            raise
        _LOGGER.info("git.reset_hard.done", extra={"workspace": workspace_path})

    def _reset_blocking(self, workspace_path: str) -> None:
        git = self._ensure_git()
        try:
            repo = git.Repo(workspace_path)
            repo.git.reset("--hard")
            repo.git.clean("-fd")
        except Exception as exc:
            raise GitOperationError(f"Reset failed: {exc!s}") from exc
