"""In-memory Corrector used for development and the TFG defence demo.

Conforms to :class:`src.core.ports.FixerPort` and produces a syntactically
valid unified diff.  To exercise the rollback path of the orchestrator the
mock can be told to fail on the N-th attempt via ``fail_on_attempts``.
"""

from __future__ import annotations

import asyncio
import logging
import textwrap

from src.core.domain import FixContext, Patch
from src.core.exceptions import FixGenerationError
from src.core.ports import FixerPort

_LOGGER = logging.getLogger(__name__)


class MockFixer(FixerPort):
    """Deterministic offline Corrector."""

    def __init__(
        self,
        *,
        artificial_latency_s: float = 0.0,
        fail_on_attempts: tuple[int, ...] = (),
    ) -> None:
        """Initialise the mock.

        Args:
            artificial_latency_s: Synthetic delay before returning.
            fail_on_attempts: Attempt indices (0-based, matching the
                ``len(previous_attempts)`` view) on which the fixer should
                raise :class:`FixGenerationError`, driving the rollback
                path deterministically.
        """
        if artificial_latency_s < 0:
            raise ValueError("artificial_latency_s must be non-negative")
        self._latency = artificial_latency_s
        self._fail_on_attempts = frozenset(fail_on_attempts)

    async def fix(self, context: FixContext) -> Patch | None:
        """See :meth:`FixerPort.fix`."""
        attempt_index = len(context.previous_attempts)
        _LOGGER.info(
            "fixer.fix.start",
            extra={"incident_id": context.incident_id, "attempt": attempt_index},
        )
        if self._latency:
            await asyncio.sleep(self._latency)

        if attempt_index in self._fail_on_attempts:
            _LOGGER.warning("fixer.fix.injecting_failure", extra={"attempt": attempt_index})
            raise FixGenerationError(f"scripted fixer failure on attempt {attempt_index}")

        diff_text = textwrap.dedent(
            """\
            --- a/src/example.py
            +++ b/src/example.py
            @@ -1,3 +1,3 @@
             def add(a, b):
            -    return a - b  # bug
            +    return a + b
            """
        )
        patch = Patch(diff_text=diff_text, author_agent=type(self).__name__)
        _LOGGER.info(
            "fixer.fix.done", extra={"attempt": attempt_index, "bytes": len(patch.diff_text)}
        )
        return patch
