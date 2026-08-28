"""Infrastructure ports.

Hexagonal architecture: the application core never imports the
``docker`` or ``git`` packages directly. Instead it depends on these
protocols, and the adapters in :mod:`src.infrastructure` provide
concrete implementations.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.domain import SandboxResult


@runtime_checkable
class SandboxPort(Protocol):
    """Ephemeral, network-isolated execution environment."""

    async def run_tests(
        self,
        workspace_path: str,
        image: str,
        command: tuple[str, ...],
    ) -> SandboxResult:
        """Execute ``command`` inside an isolated container.

        The implementation MUST enforce:

        * ``network_disabled=True``
        * an explicit memory limit (e.g. ``mem_limit="512m"``)
        * a wall-clock timeout that kills the container if exceeded
        * read-only mounting of the workspace where possible

        Args:
            workspace_path: Absolute host path to mount inside the
                container.
            image: Container image tag.
            command: Argv-style command to execute.

        Returns:
            A :class:`SandboxResult` describing the verdict and logs.

        Raises:
            DockerSandboxError: On infrastructure issues (image not
                found, daemon unreachable, ...). Functional test
                failures do NOT raise, they are encoded in the verdict.
        """
        ...


@runtime_checkable
class GitPort(Protocol):
    """Abstraction over the ``git`` working tree used by the pipeline.

    The Dev agent modifies files in-place; the orchestrator therefore
    only needs to *commit* a successful attempt or *reset* a failed one.
    There is no separate apply step.
    """

    async def commit(
        self,
        workspace_path: str,
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """Stage all changes and create a commit.

        Returns:
            The SHA of the freshly created commit.

        Raises:
            GitOperationError: If staging or committing fails.
        """
        ...

    async def reset_hard(self, workspace_path: str) -> None:
        """Discard all working-tree changes (``git reset --hard``)."""
        ...
