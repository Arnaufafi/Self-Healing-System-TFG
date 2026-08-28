"""Agent ports.

These protocols define the contracts the orchestrator depends on. By
using :class:`typing.Protocol` we get structural typing — concrete
agents (LLM-backed, rule-based, or in-memory mocks) only need to match
the signature, no ``isinstance`` registration is required.

The protocols are ``@runtime_checkable`` so tests can assert
conformance with ``isinstance(agent, FixerPort)``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.core.domain import FixContext, Patch, RegressionTest


@runtime_checkable
class FixerPort(Protocol):
    """The **Corrector**: edits the working tree in place to clear an error.

    Fix-first contract: given a failure (a crash traceback or a failing
    test) and the relevant context, the implementation modifies the
    workspace files directly.  By the time it returns, the edits are on
    disk; the returned :class:`Patch` is the unified diff of those edits,
    kept for audit / commit / post-mortem.

    Returns ``None`` (or raises :class:`FixGenerationError`) when no fix
    could be produced — the orchestrator converts either into a rollback.
    """

    async def fix(self, context: FixContext) -> Patch | None:
        """Edit the workspace to clear the error described by *context*.

        Args:
            context: The failure output, reproduction command, optional
                pre-loaded source and the tails of prior failed attempts.

        Returns:
            A :class:`Patch` of the in-place edits, or ``None`` when the
            agent produced no change.

        Raises:
            FixGenerationError: If the agent failed irrecoverably.
        """
        ...


@runtime_checkable
class TesterPort(Protocol):
    """The **Tester**: writes a regression test that immunises a fixed bug.

    Invoked *after* a green fix on the crash-entry path only.  The returned
    test encodes the CORRECT behaviour (it must pass on the fixed tree and
    would have failed before), and is appended to the session regression
    file by the orchestrator.
    """

    async def write_regression_test(self, context: FixContext) -> RegressionTest:
        """Synthesise a pytest regression test for the just-fixed error.

        Raises:
            TestGenerationError: If no test could be produced.
        """
        ...
