"""Contract for :mod:`src.core.domain.signature`.

These tests pin the single bit the orchestrator relies on: *same error*
vs *different error*.  They guard the stability of the fingerprint against
volatile noise (line numbers, hex addresses) and its sensitivity to the
things that genuinely distinguish bugs (exception type, location, message).
"""

from __future__ import annotations

from src.core.domain.signature import (
    ErrorSignature,
    from_crash_text,
    from_pytest_output,
    parse_error,
)

_CRASH = (
    "Traceback (most recent call last):\n"
    '  File "/ws/main.py", line 7, in run\n'
    "    print('Average:', average(data))\n"
    '  File "/ws/mathutils.py", line 5, in average\n'
    "    return total / len(number)\n"
    "NameError: name 'number' is not defined\n"
)


# --- crash parsing -----------------------------------------------------------


def test_crash_extracts_type_innermost_frame_and_message() -> None:
    sig = from_crash_text(_CRASH, workspace_root="/ws")
    assert sig is not None
    assert sig.kind == "crash"
    assert sig.exc_type == "NameError"
    # Innermost *in-workspace* frame wins over the outer main.py frame.
    assert sig.location == "mathutils.py:average"
    assert "number" in sig.normalized_msg


def test_same_crash_twice_matches() -> None:
    a = from_crash_text(_CRASH, "/ws")
    b = from_crash_text(_CRASH, "/ws")
    assert a is not None and b is not None
    assert a.matches(b)
    assert a.fingerprint == b.fingerprint


def test_line_number_and_address_jitter_still_matches() -> None:
    """Volatile noise (frame line numbers, a changing address) must not
    look like a new bug."""
    run_1 = (
        '  File "/ws/svc.py", line 7, in handle\n'
        "    obj.save()\n"
        "RuntimeError: dangling handle at 0xAAAA111\n"
    )
    run_2 = (
        '  File "/ws/svc.py", line 88, in handle\n'  # different frame line
        "    obj.save()\n"
        "RuntimeError: dangling handle at 0xBBBB222\n"  # same error, different address
    )
    a = from_crash_text(run_1, "/ws")
    b = from_crash_text(run_2, "/ws")
    assert a is not None and b is not None
    assert a.matches(b)


def test_same_type_and_location_different_message_matches() -> None:
    """Real benchmark case: a half-fixed IndentationError reports a different
    message on re-run but is the SAME broken spot — must not look chained."""
    run_1 = (
        '  File "calc.py", line 12\n'
        "    x = 1\n"
        "IndentationError: unexpected indent\n"
    )
    run_2 = (
        '  File "calc.py", line 7\n'
        "    y = 2\n"
        "IndentationError: unindent does not match any outer indentation level\n"
    )
    a = from_crash_text(run_1, "/ws")
    b = from_crash_text(run_2, "/ws")
    assert a is not None and b is not None
    assert a.matches(b)


def test_different_exception_type_does_not_match() -> None:
    other = _CRASH.replace("NameError", "TypeError")
    a = from_crash_text(_CRASH, "/ws")
    b = from_crash_text(other, "/ws")
    assert a is not None and b is not None
    assert not a.matches(b)


def test_different_location_does_not_match() -> None:
    """Same exception type, different innermost frame ⇒ different error."""
    relocated = (
        "Traceback (most recent call last):\n"
        '  File "/ws/other.py", line 3, in helper\n'
        "    boom()\n"
        "NameError: name 'number' is not defined\n"
    )
    a = from_crash_text(_CRASH, "/ws")
    b = from_crash_text(relocated, "/ws")
    assert a is not None and b is not None
    assert not a.matches(b)


def test_syntax_error_without_func_frame_parses() -> None:
    text = (
        '  File "/ws/broken.py", line 3\n'
        "    def bad(:\n"
        "           ^\n"
        "SyntaxError: invalid syntax\n"
    )
    sig = from_crash_text(text, "/ws")
    assert sig is not None
    assert sig.exc_type == "SyntaxError"
    assert sig.location == "broken.py"


def test_empty_crash_text_is_none() -> None:
    assert from_crash_text("   \n  ", "/ws") is None
    assert from_crash_text("", "/ws") is None


def test_uncharacterisable_output_is_none() -> None:
    """A sandbox infra line with no exception and no frame is not a bug."""
    assert from_crash_text("[in-memory sandbox] verdict=failed", "/ws") is None


# --- pytest parsing ----------------------------------------------------------

_PYTEST_FAIL = (
    "______________________________ test_average _______________________________\n"
    "tests/test_x.py:10: in test_average\n"
    "    assert average([1, 2]) == 1.5\n"
    "E   NameError: name 'number' is not defined\n"
    "=========================== short test summary info ===========================\n"
    "FAILED tests/test_x.py::test_average - NameError: name 'number' is not defined\n"
)


def test_pytest_failure_uses_nodeid_as_location() -> None:
    sig = from_pytest_output(_PYTEST_FAIL, workspace_root="/ws")
    assert sig is not None
    assert sig.kind == "test"
    assert sig.exc_type == "NameError"
    assert sig.location == "tests/test_x.py::test_average"


def test_pytest_truncated_summary_prefers_full_exc_from_error_line() -> None:
    """No TTY => pytest truncates the summary ("FileNotFoundError" -> "F..."); the
    full class name must be recovered from the untruncated ``E`` line."""
    text = (
        "test_db.py:14: in test_missing\n"
        "    with open(self.ruta, 'r') as f:\n"
        "E   FileNotFoundError: [Errno 2] No such file or directory: 'x.json'\n"
        "=========================== short test summary info ===========================\n"
        "FAILED test_db.py::test_missing - F...\n"
    )
    sig = from_pytest_output(text, workspace_root="/ws")
    assert sig is not None
    assert sig.exc_type == "FileNotFoundError"  # not the truncated "F"
    assert sig.location == "test_db.py::test_missing"


def test_pytest_green_output_is_none() -> None:
    assert from_pytest_output("==== 3 passed in 0.12s ====") is None


def test_pytest_empty_is_none() -> None:
    assert from_pytest_output("") is None


# --- dispatcher --------------------------------------------------------------


def test_parse_error_dispatches_on_kind() -> None:
    crash = parse_error(_CRASH, "crash", "/ws")
    test = parse_error(_PYTEST_FAIL, "test", "/ws")
    assert isinstance(crash, ErrorSignature) and crash.kind == "crash"
    assert isinstance(test, ErrorSignature) and test.kind == "test"
