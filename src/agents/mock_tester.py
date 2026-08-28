"""In-memory Tester used for development and the TFG defence demo.

Conforms to :class:`src.core.ports.TesterPort` and returns a minimal,
syntactically valid pytest function.  Its only purpose is to exercise the
immunization plumbing; the orchestrator appends ``source`` to the session
regression file and derives the real node id.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import textwrap

from src.core.domain import FixContext, RegressionTest
from src.core.ports import TesterPort

_LOGGER = logging.getLogger(__name__)


class MockTester(TesterPort):
    """Deterministic, fast, offline Tester."""

    def __init__(self, *, artificial_latency_s: float = 0.0) -> None:
        """Initialise the mock with an optional synthetic latency."""
        if artificial_latency_s < 0:
            raise ValueError("artificial_latency_s must be non-negative")
        self._latency = artificial_latency_s

    async def write_regression_test(self, context: FixContext) -> RegressionTest:
        """See :meth:`TesterPort.write_regression_test`."""
        _LOGGER.info("tester.write.start", extra={"incident_id": context.incident_id})
        if self._latency:
            await asyncio.sleep(self._latency)

        digest = hashlib.sha256(context.failure_output.encode("utf-8")).hexdigest()[:8]
        func = f"test_selfheal_{digest}"
        source = textwrap.dedent(
            f"""\
            def {func}() -> None:
                # Immunization test for incident {context.incident_id}
                # (digest {digest}); passes now that the bug is fixed.
                assert True
            """
        )
        # path / node_id are advisory — the immunize node overrides them with
        # the session regression file and the real first-test-function name.
        result = RegressionTest(path="", node_id=f"::{func}", source=source)
        _LOGGER.info("tester.write_regression_test.done", extra={"func": func})
        return result
