"""Concrete agent implementations.

Mock agents (Corrector / Tester / Reporter) are always available.  Real
agents are gated behind lazy imports so the project remains runnable without
``mini-swe-agent`` / ``litellm`` installed:

* :class:`MiniSWEFixer`  — Corrector backed by mini-swe-agent.
* :class:`LLMTester`     — Tester backed by litellm.
* :class:`LLMReporter`   — Reporter backed by litellm.
"""

from src.agents.mock_fixer import MockFixer
from src.agents.mock_reporter_agent import MockReporterAgent
from src.agents.mock_tester import MockTester

__all__ = [
    "LLMReporter",
    "LLMTester",
    "MiniSWEFixer",
    "MockFixer",
    "MockReporterAgent",
    "MockTester",
]


def __getattr__(name: str) -> type:
    """Lazy import real agents only when explicitly requested."""
    if name == "MiniSWEFixer":
        from src.agents.swe_agent_dev import MiniSWEFixer
        return MiniSWEFixer
    if name == "LLMTester":
        from src.agents.llm_tester import LLMTester
        return LLMTester
    if name == "LLMReporter":
        from src.agents.llm_reporter import LLMReporter
        return LLMReporter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
