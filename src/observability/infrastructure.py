"""GoF decorators that instrument the infrastructure ports (sandbox, git)."""

from __future__ import annotations

from src.core.domain import SandboxResult
from src.core.ports import GitPort, SandboxPort, TelemetryPort
from src.observability.span import span


class InstrumentedSandbox(SandboxPort):
    """Times :meth:`SandboxPort.run_tests` and tags the resulting verdict."""

    def __init__(self, inner: SandboxPort, sink: TelemetryPort) -> None:
        """Wrap *inner*, recording spans to *sink*."""
        self._inner = inner
        self._sink = sink

    async def run_tests(
        self, workspace_path: str, image: str, command: tuple[str, ...]
    ) -> SandboxResult:
        """See :meth:`SandboxPort.run_tests`."""
        async with span(self._sink, "sandbox.run_tests", command=" ".join(command)) as attrs:
            result = await self._inner.run_tests(workspace_path, image, command)
            attrs["verdict"] = result.verdict.value
            return result


class InstrumentedGit(GitPort):
    """Times :meth:`GitPort.commit` and :meth:`GitPort.reset_hard`."""

    def __init__(self, inner: GitPort, sink: TelemetryPort) -> None:
        """Wrap *inner*, recording spans to *sink*."""
        self._inner = inner
        self._sink = sink

    async def commit(
        self, workspace_path: str, message: str, author_name: str, author_email: str
    ) -> str:
        """See :meth:`GitPort.commit`."""
        async with span(self._sink, "git.commit"):
            return await self._inner.commit(workspace_path, message, author_name, author_email)

    async def reset_hard(self, workspace_path: str) -> None:
        """See :meth:`GitPort.reset_hard`."""
        async with span(self._sink, "git.reset_hard"):
            await self._inner.reset_hard(workspace_path)
