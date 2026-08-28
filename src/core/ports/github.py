"""GitHub deployment port.

The real-environment entry point (heal a repository → open a Pull Request)
depends on this protocol, not on the GitHub REST API or the ``gh`` CLI
directly.  The concrete adapter lives in :mod:`src.infrastructure.github`.

The contract is deliberately small and **never merges**: it pushes a branch
and opens a PR, leaving the merge decision to a human reviewer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GitHubPort(Protocol):
    """Push a healed branch and open a pull request (no auto-merge)."""

    async def push_branch(
        self, *, workspace_path: str, repo: str, branch: str, force: bool = True
    ) -> None:
        """Push the current ``HEAD`` of *workspace_path* as *branch*.

        The remote is ``github.com/{repo}``.

        Raises:
            GitHubError: If the push fails.
        """
        ...

    async def open_pull_request(
        self, *, repo: str, base: str, head: str, title: str, body: str, draft: bool = False
    ) -> str:
        """Open a pull request (``head`` → ``base``) and return its HTML URL.

        Never merges — the PR is left for human review.

        Raises:
            GitHubError: If the API call fails.
        """
        ...
