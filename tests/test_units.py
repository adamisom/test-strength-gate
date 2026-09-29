"""Unit tests for the pure parts: globs, test IDs, fingerprints, rules, summaries."""

import pytest

from test_strength_gate.classify import (BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, STRONG, WEAK,
                                         Outcome, failure_kind, summarize, verdict)
from test_strength_gate.gate import JudgedTest
from test_strength_gate.report import LEGEND, summary_line
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
    ("tests/data.json", True),       # data files travel with the tests
    ("tests/data/cases/x.robot", True),
    ("pkg/tests/data.json", False),
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


def test_fingerprint_ignores_docstring_edits():
    source = "def test_d():\n    \"\"\"Old words.\"\"\"\n    assert 1\n"
    parts = ["test_d"]
    assert function_fingerprint(source, parts) == function_fingerprint(source.replace("Old", "New"), parts)
    assert function_fingerprint(source, parts) == function_fingerprint("def test_d():\n    assert 1\n", parts)
    assert function_fingerprint(source, parts) != function_fingerprint(source.replace("1", "2"), parts)


def test_fingerprint_sees_decorator_changes():
    changed = SOURCE.replace("[1, 2]", "[1, 2, 3]")
    parts = ["TestThing", "test_n"]
    assert function_fingerprint(SOURCE, parts) != function_fingerprint(changed, parts)


CLASS_SOURCE = '''
import pytest

class TestThing:
    """Docs."""
    limit = 3

    def setup_method(self):
        self.items = []

    def helper(self):
        return 1

    def test_a(self):
        assert self.items == []

    def test_b(self):
        assert self.limit == 3
'''


@pytest.mark.parametrize("old, new, changed", [
    # Codex review finding 6: edits to the class itself change every test in it.
    ("class TestThing:", "@pytest.mark.usefixtures('db')\nclass TestThing:", True),
    ("class TestThing:", "class TestThing(Base):", True),
    ("limit = 3", "limit = 4", True),
    ("self.items = []", "self.items = [0]", True),
    ("    def helper(self):", "    @pytest.fixture(autouse=True)\n    def helper(self):", True),
    # Edits to a sibling test, a plain helper or the class docstring don't.
    ("assert self.limit == 3", "assert self.limit == 4", False),
    ("return 1", "return 2", False),
    ('"""Docs."""', '"""Other docs."""', False),
])
def test_fingerprint_includes_the_enclosing_class_context(old, new, changed):
    parts = ["TestThing", "test_a"]
    edited = CLASS_SOURCE.replace(old, new)
    assert edited != CLASS_SOURCE
    assert (function_fingerprint(CLASS_SOURCE, parts) != function_fingerprint(edited, parts)) is changed


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
    # Fable audit TSG-1: other wordings Python and C functions use for a call
    # that doesn't fit the signature, e.g. a test calling a new signature.
    ("TypeError", "f() got multiple values for argument 'b'", "missing_api"),
    ("TypeError", "f() got some positional-only arguments passed as keyword arguments: 'b'", "missing_api"),
    ("TypeError", "f() takes exactly 2 arguments (3 given)", "missing_api"),
    ("TypeError", "f() takes at most 2 arguments (3 given)", "missing_api"),
    ("TypeError", "f() takes at least 1 argument (0 given)", "missing_api"),
    ("TypeError", "dict.get() takes no keyword arguments", "missing_api"),
    ("TypeError", "f expected at most 2 arguments, got 3", "missing_api"),
    ("TypeError", "f expected at least 1 argument, got 0", "missing_api"),
    ("TypeError", "f expected exactly 2 arguments, got 1", "missing_api"),
    ("TypeError", "divmod expected 2 arguments, got 1", "missing_api"),
    ("TypeError", "len() takes exactly one argument (2 given)", "missing_api"),
    # Codex TSG-16: a TypeError the project raises itself, whose words happen to
    # match a signature message, is behavior, not a call that doesn't fit.
    ("TypeError", "renderer takes at most 2 arguments for this input", "other_error"),
    ("TypeError", "the handler expected 2 arguments, got 3 in config", "other_error"),
    ("TypeError", "Foo.__init__() got an unexpected keyword argument 'c'", "missing_api"),
    ("TypeError", "<lambda>() takes 1 positional argument but 2 were given", "missing_api"),
    ("TypeError", "f() missing 1 required keyword-only argument: 'k'", "missing_api"),
    # Fable audit 2, TSG-17: Django's and SQLAlchemy's wordings for a model
    # field that doesn't exist yet. TSG-21: CPython's Argument Clinic wordings.
    ("TypeError", "Author() got unexpected keyword arguments: 'nickname'", "missing_api"),
    ("TypeError", "Author() got unexpected keyword argument: 'nickname'", "missing_api"),
    ("TypeError", "'nickname' is an invalid keyword argument for Author", "missing_api"),
    ("TypeError", "'strict' is an invalid keyword argument for int()", "missing_api"),
    ("TypeError", "open() missing required argument 'file' (pos 1)", "missing_api"),
    ("TypeError", "this function got an unexpected keyword argument 'nope'", "missing_api"),
    ("TypeError", "the value 'x' is an invalid keyword argument for this form", "other_error"),
    ("TypeError", "unsupported operand type(s) for +: 'int' and 'str'", "other_error"),
    ("TypeError", "expected str, bytes or os.PathLike object, not NoneType", "other_error"),
    ("ValueError", "bad input", "other_error"),
])
def test_failure_kind(exc_type, message, kind):
    assert failure_kind(exc_type, message) == kind


def test_a_project_typeerror_that_only_sounds_like_a_signature_error_is_strong():
    """Codex TSG-16: old project code raising its own TypeError is a behavioral failure."""
    msg = "renderer takes at most 2 arguments for this input"
    assert failure_kind("TypeError", msg, origin="project") == "other_error"
    base = Outcome("failed", "call", "TypeError", msg, raised_at="lib.py:3", raised_inside=True, origin="project")
    assert verdict(base, PASSED)[0] == STRONG


@pytest.mark.parametrize("origin, kind", [
    ("test", "missing_api"),       # the test's own call has the wrong arguments
    ("library", "missing_api"),    # e.g. a library decorator's wrapper passed them on
    ("", "missing_api"),           # unknown
    ("project", "unclear_call"),   # the old code's own call: new API or old bug
])
def test_argument_type_error_depends_on_where_it_was_raised(origin, kind):
    assert failure_kind("TypeError", "f() missing 1 required positional argument: 'b'", origin=origin) == kind


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
    # Fable audit TSG-3. A test that was not collected at head, or collected
    # but not run there, has no head result, so it is not shown to fail there.
    for head, words in [(summarize(None), "Not collected at head"),
                        (summarize(None, collected=True), "Collected at head but not run")]:
        label, reason = verdict(base, head)
        assert label == INCONCLUSIVE and reason.startswith(words)


def test_collected_but_not_run_at_base_has_its_own_reason():
    # Fable audit TSG-3. With -x in addopts the session used to stop at the
    # first failure, and the later tests were called "not collected at base".
    label, reason = verdict(summarize(None, collected=True), PASSED)
    assert label == INCONCLUSIVE and reason.startswith("Collected at base but not run")
    assert verdict(summarize(None), PASSED)[1].startswith("Not collected at base")
    assert summarize(None, collected=True).describe() == "not run"
    assert summarize(None).describe() == "not collected"


def test_collect_and_startup_errors_are_inconclusive():
    label, reason = verdict(summarize(None, collect_error="ModuleNotFoundError: x"), PASSED)
    assert (label, reason) == (INCONCLUSIVE, "The file fails to import at base: ModuleNotFoundError: x")
    assert verdict(summarize(None, startup_error="boom"), PASSED)[0] == INCONCLUSIVE


def test_a_head_run_that_fails_as_a_whole_is_inconclusive_not_broken():
    # Codex re-review, remaining 2. A timeout or an internal pytest error at
    # head says nothing about the test itself, so it must not be BROKEN_AT_HEAD.
    base = summarize({"call": phase("failed", "AssertionError", "assert 0")})
    label, reason = verdict(base, summarize(None, startup_error="pytest timed out after 900 seconds"))
    assert label == INCONCLUSIVE
    assert "at head" in reason and "pytest timed out after 900 seconds" in reason


def test_strong_from_another_exception_is_worded_as_conditional():
    # Codex re-review, remaining 3. The verdict rule is unchanged, but the
    # reason must not sound conclusive: the reviewer has to check the cause.
    base = summarize({"call": {**phase("failed", "KeyError", "'total'"), "raised_at": "src/app.py:7",
                               "raised_inside": True, "local_at": "src/app.py:7"}})
    label, reason = verdict(base, PASSED)
    assert label == STRONG
    assert reason == ("Fails at base with KeyError raised at src/app.py:7: 'total'. Not a check, "
                      "so inspect the cause: this is strong only if the old code caused it.")


def judged(verdict_, kind="added"):
    return JudgedTest("t.py::x", kind, PASSED, PASSED, verdict_, "")


def test_summary_line():
    assert summary_line([judged(STRONG), judged(STRONG), judged(WEAK)]) == \
        "3 new tests: 2 strong, 1 weak (passes without the source change), 0 inconclusive"
    assert summary_line([judged(WEAK, "modified"), judged(WEAK), judged(BROKEN_AT_HEAD)]) == (
        "3 new or changed tests (2 new, 1 modified): 0 strong, 2 weak (pass without the source change), "
        "0 inconclusive, 1 broken at head")
    assert summary_line([]) == "No added or modified tests to judge."
    broken_file = JudgedTest("tests/test_x.py", "added", PASSED, PASSED, BROKEN_AT_HEAD, "")
    assert summary_line([broken_file]) == (
        "No tests could be judged: 1 changed test file fails to import at head, so its tests were not judged.")


def test_summary_line_counts_files_whose_head_run_failed_apart_from_broken_files():
    broken_file = JudgedTest("tests/test_x.py", "added", PASSED, PASSED, BROKEN_AT_HEAD, "")
    run_failed = JudgedTest("tests/test_y.py", "added", PASSED, PASSED, INCONCLUSIVE, "")
    assert summary_line([run_failed]) == (
        "No tests could be judged: pytest failed as a whole at head for 1 changed test file, "
        "so its tests were not judged.")
    assert summary_line([judged(STRONG), broken_file, run_failed]) == (
        "1 new test: 1 strong, 0 weak (pass without the source change), 0 inconclusive. "
        "Also, 1 changed test file fails to import at head, and pytest failed as a whole at head "
        "for 1 changed test file, so their tests were not judged.")


def test_legend_covers_the_newer_rules():
    # Codex review finding 9 asked for wording that matches what the tool does.
    # The legend under every report must mention the missing-file rule and the
    # file rows for test files that fail to import at head.
    assert "a file that exists at head but not in the base run" in LEGEND
    assert "a file path instead of a test" in LEGEND


def test_legend_does_not_claim_file_rows_were_run_twice():
    # Codex re-review, finding 9 nit. A file row is not a test, and its tests
    # may never have run, so the legend must not say every row ran twice.
    assert "Each test above was run twice" not in LEGEND
    first, rest = LEGEND.split("\n\n", 2)[1:]
    assert first.startswith("Each judged test was run twice")
    assert "BROKEN_AT_HEAD if the file fails to import at head" in first
    assert "INCONCLUSIVE if the pytest run at head failed as a whole" in first
    assert "Another exception counts only if the old code caused it" in rest
