"""Unit tests for the pure parts: globs, test IDs, fingerprints, rules, summaries."""

import pytest

from test_strength_gate.classify import (BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, STRONG, WEAK,
                                         Outcome, failure_kind, summarize, verdict)
from test_strength_gate.gate import JudgedTest
from test_strength_gate.report import summary_line
from test_strength_gate.selection import (DEFAULT_GLOBS, function_fingerprint, matches_any,
                                          pick_judged, split_test_id)


@pytest.mark.parametrize("path, expected", [
    ("test_x.py", True),
    ("pkg/test_x.py", True),
    ("pkg/x_test.py", True),
    ("tests/helpers.py", True),
    ("tests/unit/deep/helpers.py", True),
    ("conftest.py", True),
    ("pkg/sub/conftest.py", True),
    ("pkg/tests/helpers.py", False),  # patterns with a slash are anchored at the root
    ("src/app.py", False),
    ("tests/data.json", False),
    ("testing.py", False),
])
def test_default_globs(path, expected):
    assert matches_any(path, DEFAULT_GLOBS) is expected


def test_single_star_stays_in_one_directory():
    assert matches_any("tests/test_a.py", ["tests/*.py"])
    assert not matches_any("tests/sub/test_a.py", ["tests/*.py"])


def test_split_test_id():
    assert split_test_id("tests/t.py::TestA::test_b[1-x::y]") == ("tests/t.py", ["TestA", "test_b"])
    assert split_test_id("t.py::test_c") == ("t.py", ["test_c"])


SOURCE = '''
import pytest

class TestThing:
    @pytest.mark.parametrize("n", [1, 2])
    def test_n(self, n):
        assert n
'''


def test_fingerprint_ignores_comments_and_formatting():
    reformatted = SOURCE.replace("assert n", "# a comment\n        assert   n")
    parts = ["TestThing", "test_n"]
    assert function_fingerprint(SOURCE, parts) == function_fingerprint(reformatted, parts)


def test_fingerprint_sees_decorator_changes():
    changed = SOURCE.replace("[1, 2]", "[1, 2, 3]")
    parts = ["TestThing", "test_n"]
    assert function_fingerprint(SOURCE, parts) != function_fingerprint(changed, parts)


def test_fingerprint_missing_function_or_file():
    assert function_fingerprint(SOURCE, ["test_absent"]) is None
    assert function_fingerprint(None, ["test_n"]) is None
    assert function_fingerprint("def broken(:", ["broken"]) is None


def test_pick_judged():
    head = {"t.py": "def test_a():\n    assert 2\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n"}
    base = {"t.py": "def test_a():\n    assert 1\n\ndef test_b():\n    pass\n"}
    judged = pick_judged(["t.py::test_a", "t.py::test_b", "t.py::test_c"], ["t.py::test_a", "t.py::test_b"],
                         head, base)
    assert judged == {"t.py::test_a": "modified", "t.py::test_c": "added"}


@pytest.mark.parametrize("exc_type, message, kind", [
    ("AssertionError", "assert 1 == 2", "assertion"),
    ("AttributeError", "monkeypatch: <module 'calc'> has no attribute 'x'", "missing_api"),
    ("Failed", "DID NOT RAISE ValueError", "assertion"),
    ("ImportError", "cannot import name 'x'", "missing_api"),
    ("ModuleNotFoundError", "No module named 'x'", "missing_api"),
    ("AttributeError", "module 'calc' has no attribute 'mul'", "missing_api"),
    ("NameError", "name 'x' is not defined", "missing_api"),
    ("TypeError", "add() got an unexpected keyword argument 'c'", "missing_api"),
    ("TypeError", "add() takes 2 positional arguments but 3 were given", "missing_api"),
    ("TypeError", "add() missing 1 required positional argument: 'b'", "missing_api"),
    ("TypeError", "unsupported operand type(s) for +: 'int' and 'str'", "other_error"),
    ("ValueError", "bad input", "other_error"),
])
def test_failure_kind(exc_type, message, kind):
    assert failure_kind(exc_type, message) == kind


@pytest.mark.parametrize("owner, kind", [
    ("module", "missing_api"),    # calc.new_func
    ("class", "missing_api"),     # Thing.new_classmethod
    ("object", "missing_api"),    # thing.new_method
    ("builtin", "other_error"),   # None.value: base returned the wrong kind of value
    (None, "missing_api"),        # raised by hand (monkeypatch, mock.patch), owner unknown
])
def test_attribute_error_depends_on_owner(owner, kind):
    assert failure_kind("AttributeError", "has no attribute 'x'", owner) == kind


def phase(outcome, exc_type=None, message=None):
    return {"outcome": outcome, "exc_type": exc_type, "message": message, "xfail": False}


PASSED = Outcome("passed", "call")


@pytest.mark.parametrize("base_phases, expected", [
    ({"setup": phase("passed"), "call": phase("failed", "AssertionError", "assert 0"),
      "teardown": phase("passed")}, STRONG),
    ({"setup": phase("passed"), "call": phase("failed", "KeyError", "'k'")}, STRONG),
    ({"setup": phase("passed"), "call": phase("passed"), "teardown": phase("passed")}, WEAK),
    ({"setup": phase("failed", "ValueError", "fixture broke")}, INCONCLUSIVE),
    ({"setup": phase("passed"), "call": phase("passed"), "teardown": phase("failed", "OSError")}, INCONCLUSIVE),
    ({"setup": phase("skipped", "Skipped", "no")}, SKIPPED),
    (None, INCONCLUSIVE),
])
def test_verdict_from_base_phases(base_phases, expected):
    assert verdict(summarize(base_phases), PASSED)[0] == expected


def test_head_failure_overrides_a_strong_base():
    base = summarize({"call": phase("failed", "AssertionError", "assert 0")})
    head = summarize({"setup": phase("failed", "RuntimeError", "db down")})
    assert verdict(base, head)[0] == BROKEN_AT_HEAD
    assert verdict(base, summarize(None))[0] == BROKEN_AT_HEAD  # not collected at head


def test_collect_and_startup_errors_are_inconclusive():
    label, reason = verdict(summarize(None, collect_error="ModuleNotFoundError: x"), PASSED)
    assert (label, reason) == (INCONCLUSIVE, "The file fails to import at base: ModuleNotFoundError: x")
    assert verdict(summarize(None, startup_error="boom"), PASSED)[0] == INCONCLUSIVE


def judged(verdict_, kind="added"):
    return JudgedTest("t.py::x", kind, PASSED, PASSED, verdict_, "")


def test_summary_line():
    assert summary_line([judged(STRONG), judged(STRONG), judged(WEAK)]) == \
        "3 new tests: 2 strong, 1 weak (passes without the source change), 0 inconclusive"
    assert summary_line([judged(WEAK, "modified"), judged(WEAK), judged(BROKEN_AT_HEAD)]) == (
        "3 new or changed tests (2 new, 1 modified): 0 strong, 2 weak (pass without the source change), "
        "0 inconclusive, 1 broken at head")
    assert summary_line([]) == "No added or modified tests to judge."
