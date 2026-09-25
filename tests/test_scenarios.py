"""End-to-end: build one PR that exercises every verdict, run the gate once, check each test."""

import pytest

from test_strength_gate.classify import BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, STRONG, WEAK
from helpers import GitRepo

BASE_FILES = {
    "calc.py": """
        def add(a, b):
            return a + b

        def is_even(n):
            return n % 2 == 1  # bug: fixed in the PR

        def clamp(x, lo, hi):
            return max(lo, min(x, hi))

        def sign(x):
            return 1 if x > 0 else -1  # bug: sign(0) should be 0
    """,
    "tests/test_calc.py": """
        import pytest
        import calc

        def test_add_basic():
            assert calc.add(1, 2) == 3

        @pytest.mark.parametrize("a, b, total", [(1, 1, 2), (2, 2, 4)])
        def test_add_table(a, b, total):
            assert calc.add(a, b) == total

        def test_clamp():
            assert calc.clamp(5, 0, 3) == 3
    """,
}

HEAD_FILES = {
    "calc.py": """
        def add(a, b, c=0):
            return a + b + c

        def is_even(n):
            return n % 2 == 0

        def clamp(x, lo, hi):
            if lo > hi:
                raise ValueError("lo must not exceed hi")
            return max(lo, min(x, hi))

        def sign(x):
            if x == 0:
                return 0
            return 1 if x > 0 else -1

        def mul(a, b):
            return a * b
    """,
    "textutil.py": """
        def shout(s):
            return s.upper() + "!"
    """,
    "tests/conftest.py": """
        import pytest

        @pytest.fixture
        def small_numbers():
            return [1, 2, 3]
    """,
    "tests/test_calc.py": """
        import pytest
        import calc

        def test_add_basic():
            # A comment-only edit: the function is unchanged, so it is not judged.
            assert calc.add(1, 2) == 3

        @pytest.mark.parametrize("a, b, total", [(1, 1, 2), (2, 2, 4), (0, 0, 0)])
        def test_add_table(a, b, total):
            assert calc.add(a, b) == total

        def test_clamp():
            assert calc.clamp(5, 0, 3) == 3
            with pytest.raises(ValueError):
                calc.clamp(1, 5, 0)

        def test_is_even():
            assert calc.is_even(4)

        def test_add_zero():
            assert calc.add(5, 0) == 5

        def test_mul():
            from calc import mul
            assert mul(2, 3) == 6

        def test_add_three():
            assert calc.add(1, 2, c=3) == 6

        @pytest.mark.parametrize("x, expected", [(5, 1), (-5, -1), (0, 0)])
        def test_sign(x, expected):
            assert calc.sign(x) == expected

        def test_add_negative():
            assert calc.add(-1, -1) == -3  # wrong expectation: fails at head too

        def test_sum_fixture(small_numbers):
            assert sum(small_numbers) == 6

        @pytest.mark.skip(reason="not ready")
        def test_later():
            assert False
    """,
    "tests/test_textutil.py": """
        from textutil import shout

        def test_shout():
            assert shout("hi") == "HI!"
    """,
}


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    repo = GitRepo(tmp_path_factory.mktemp("scenario") / "repo")
    base = repo.commit(BASE_FILES, "base")
    head = repo.commit(HEAD_FILES, "the PR")
    return repo.gate(base, head)


@pytest.fixture(scope="module")
def by_id(result):
    return {t.id.removeprefix("tests/"): t for t in result.tests}


def check(by_id, test_id, verdict, kind="added"):
    t = by_id[test_id]
    assert (t.verdict, t.kind) == (verdict, kind), t


def test_a_assertion_failure_at_base_is_strong(by_id):
    check(by_id, "test_calc.py::test_is_even", STRONG)
    assert "Fails at base on a check" in by_id["test_calc.py::test_is_even"].reason


def test_b_passing_at_base_is_weak(by_id):
    check(by_id, "test_calc.py::test_add_zero", WEAK)


def test_c_importing_a_missing_function_is_inconclusive(by_id):
    check(by_id, "test_calc.py::test_mul", INCONCLUSIVE)
    assert by_id["test_calc.py::test_mul"].base.exc_type == "ImportError"


def test_c_new_keyword_argument_is_inconclusive(by_id):
    check(by_id, "test_calc.py::test_add_three", INCONCLUSIVE)
    assert by_id["test_calc.py::test_add_three"].base.exc_type == "TypeError"


def test_d_new_module_collection_error_is_inconclusive(by_id):
    t = by_id["test_textutil.py::test_shout"]
    check(by_id, "test_textutil.py::test_shout", INCONCLUSIVE)
    assert t.base.phase == "collect"
    assert "textutil" in t.reason


def test_e_modified_test_did_not_raise_is_strong(by_id):
    check(by_id, "test_calc.py::test_clamp", STRONG, kind="modified")
    assert "DID NOT RAISE" in by_id["test_calc.py::test_clamp"].reason


def test_e_unchanged_test_in_changed_file_is_not_judged(by_id):
    assert "test_calc.py::test_add_basic" not in by_id


def test_f_parametrized_cases_are_judged_one_by_one(by_id):
    check(by_id, "test_calc.py::test_sign[5-1]", WEAK)
    check(by_id, "test_calc.py::test_sign[-5--1]", WEAK)
    check(by_id, "test_calc.py::test_sign[0-0]", STRONG)


def test_f_new_parametrize_case_marks_old_cases_modified(by_id):
    # Editing the decorator changes the function, so the old cases count as
    # modified even though their values did not change. See DECISIONS.md.
    check(by_id, "test_calc.py::test_add_table[0-0-0]", WEAK)
    check(by_id, "test_calc.py::test_add_table[1-1-2]", WEAK, kind="modified")


def test_g_failing_at_head_is_broken(by_id):
    check(by_id, "test_calc.py::test_add_negative", BROKEN_AT_HEAD)


def test_new_conftest_fixture_is_copied_to_base(by_id):
    check(by_id, "test_calc.py::test_sum_fixture", WEAK)


def test_skipped(by_id):
    check(by_id, "test_calc.py::test_later", SKIPPED)


def test_changed_test_files_include_conftest(result):
    assert sorted(result.test_files) == ["tests/conftest.py", "tests/test_calc.py", "tests/test_textutil.py"]
    assert result.warnings == []
