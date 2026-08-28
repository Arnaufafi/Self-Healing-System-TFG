"""Centralised exception hierarchy for the self-healing pipeline.

A flat, narrowly-scoped hierarchy makes it trivial for nodes to perform
fine-grained ``except`` clauses without depending on infrastructure
modules. All exceptions inherit from :class:`SelfHealingError` so the
top-level orchestrator can install a single safety net.
"""

from __future__ import annotations


class SelfHealingError(Exception):
    """Base class for every exception raised by this project."""


# --- Infrastructure layer ----------------------------------------------------


class InfrastructureError(SelfHealingError):
    """Raised when an external system (Docker, Git, FS) misbehaves."""


class DockerSandboxError(InfrastructureError):
    """Generic sandbox failure (container creation, network, etc.)."""


class SandboxTimeoutError(DockerSandboxError):
    """Patched code exceeded the wall-clock budget."""


class GitOperationError(InfrastructureError):
    """A git command returned a non-zero status code."""


# --- Agent layer -------------------------------------------------------------


class AgentError(SelfHealingError):
    """Base class for failures inside agent implementations."""


class FixGenerationError(AgentError):
    """The Corrector agent could not produce a fix (no edits applied)."""


class TestGenerationError(AgentError):
    """The Tester agent could not synthesise a regression test."""


class ReportGenerationError(AgentError):
    """The Reporter agent could not compose a commit message / post-mortem."""


# --- Orchestration layer -----------------------------------------------------


class OrchestrationError(SelfHealingError):
    """Generic graph-level error (invalid state, missing field, ...)."""


class MaxRetriesExceededError(OrchestrationError):
    """Convenience marker raised when the rollback budget is exhausted."""
