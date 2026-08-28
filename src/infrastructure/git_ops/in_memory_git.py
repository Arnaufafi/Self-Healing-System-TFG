"""In-memory Git adapter.

Records commits / resets without touching a real repository.  The
orchestrator only needs ``commit`` and ``reset_hard`` under the
*apply-in-place* contract — there is no patch application step to
simulate.
"""

from __future__ import annotations

import asyncio
import logging

from src.core.ports import GitPort

_LOGGER = logging.getLogger(__name__)


class InMemoryGit(GitPort):
    """Deterministic Git replacement for tests and demos."""

    def __init__(self) -> None:
        """Initialise the in-memory bookkeeping counters."""
        self.commits: list[str] = []
        self.reset_count: int = 0

    async def commit(
        self,
        workspace_path: str,
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """See :meth:`GitPort.commit`."""
        await asyncio.sleep(0)
        sha = f"sha_{len(self.commits):08d}"
        self.commits.append(message)
        _LOGGER.info("git.inmemory.commit", extra={"sha": sha})
        return sha

    async def reset_hard(self, workspace_path: str) -> None:
        """See :meth:`GitPort.reset_hard`."""
        await asyncio.sleep(0)
        self.reset_count += 1
        _LOGGER.info("git.inmemory.reset_hard", extra={"reset_count": self.reset_count})
