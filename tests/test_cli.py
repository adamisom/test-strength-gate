"""End-to-end edge cases through the CLI and the gate."""

import json
import subprocess
import sys

from test_strength_gate.classify import INCONCLUSIVE, STRONG, WEAK
from test_strength_gate.cli import main
from test_strength_gate.gate import run_gate

SRC_BASE = {"lib.py": "def double(x):\n    return x + x\n\ndef half(x):\n    return x // 2\n"}
SRC_HEAD = {"lib.py": "def double(x):\n    return 2 * x\n\ndef half(x):\n    return x / 2\n"}
WEAK_TEST = {"tests/test_lib.py": "import lib\n\ndef test_double():\n    assert lib.double(3) == 6\n"}
STRONG_TEST = {"tests/test_half.py": "import lib\n\ndef test_half():\n    assert lib.half(3) == 1.5\n"}


def cli(repo, base, head, *extra):
    return main(["--repo", str(repo.path), "--base", base, "--head", head, "--python", sys.executable, *extra])


def test_weak_test_exits_0_by_default_and_1_with_fail_on_weak(repo, tmp_path):
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, **WEAK_TEST})
    assert cli(repo, base, head) == 0
    assert cli(repo, base, head, "--fail-on", "weak") == 1


def test_strong_only_passes_fail_on_weak_and_writes_reports(repo, tmp_path, monkeypatch):
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, **STRONG_TEST})
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    code = cli(repo, base, head, "--fail-on", "weak", "--json", str(out_json), "--markdown", str(out_md))
    assert code == 0
    data = json.loads(out_json.read_text())
    assert data["counts"] == {STRONG: 1}
    assert data["tests"][0]["id"] == "tests/test_half.py::test_half"
    assert "1 new test: 1 strong" in out_md.read_text()
    assert summary.read_text() == out_md.read_text()


def test_no_test_changes(repo, capsys):
    base = repo.commit({**SRC_BASE, **WEAK_TEST})
    head = repo.commit(SRC_HEAD)
    assert cli(repo, base, head, "--fail-on", "weak") == 0
    assert "No added or modified tests to judge." in capsys.readouterr().out


def test_deleted_test_files_are_ignored(repo):
    base = repo.commit({**SRC_BASE, **WEAK_TEST})
    head = repo.commit({"tests/test_lib.py": None})
    result = repo.gate(base, head)
    assert result.test_files == [] and result.tests == []


def test_uses_merge_base_not_base_branch_tip(repo):
    fork_point = repo.commit(SRC_BASE)
    repo.git("checkout", "-q", "-b", "feature")
    head = repo.commit({**SRC_HEAD, **STRONG_TEST})
    repo.git("checkout", "-q", "main")
    # A test lands on main after the PR branched. It is not part of the PR.
    main_tip = repo.commit({"tests/test_other.py": "def test_other():\n    pass\n"})
    result = repo.gate(main_tip, head)
    assert result.base == fork_point
    assert result.test_files == ["tests/test_half.py"]
    assert [t.verdict for t in result.tests] == [STRONG]


def test_custom_glob_and_pytest_args(repo):
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, "checks/check_lib.py": WEAK_TEST["tests/test_lib.py"]})
    assert repo.gate(base, head).tests == []  # not a test file under the default globs
    result = repo.gate(base, head, globs=["checks/*.py"],
                       pytest_args=["-o", "python_files=check_*.py"])
    assert [(t.id, t.verdict) for t in result.tests] == [("checks/check_lib.py::test_double", WEAK)]


def test_conftest_that_imports_a_new_module_makes_tests_inconclusive(repo):
    base = repo.commit(SRC_BASE)
    head = repo.commit({
        **SRC_HEAD,
        "newhelpers.py": "VALUE = 6\n",
        "tests/conftest.py": "import newhelpers\n",
        **STRONG_TEST,
    })
    [test] = repo.gate(base, head).tests
    assert test.verdict == INCONCLUSIVE
    assert "newhelpers" in test.reason


def test_runs_as_module(repo):
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, **STRONG_TEST})
    proc = subprocess.run([sys.executable, "-m", "test_strength_gate", "--repo", str(repo.path),
                           "--base", base, "--head", head], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "1 new test: 1 strong" in proc.stdout


def test_bad_revision_exits_2(repo, capsys):
    head = repo.commit(SRC_BASE)
    assert cli(repo, "no-such-ref", head) == 2
    assert "no-such-ref" in capsys.readouterr().err


def test_tests_only_pr_is_noted_and_does_not_fail_on_weak(repo, tmp_path):
    base = repo.commit(SRC_BASE)
    head = repo.commit(WEAK_TEST)
    result = repo.gate(base, head)
    assert result.pr_kind == "tests_only"
    assert [t.verdict for t in result.tests] == [WEAK]
    out = tmp_path / "r.md"
    assert cli(repo, base, head, "--fail-on", "weak", "--markdown", str(out)) == 0
    assert "Weak results are normal here" in out.read_text()


def test_refactor_with_only_weak_tests_is_noted(repo, tmp_path):
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, **WEAK_TEST})
    out = tmp_path / "r.md"
    cli(repo, base, head, "--markdown", str(out))
    assert "That is expected for a refactor" in out.read_text()


def test_code_loaded_from_outside_the_worktree_makes_base_inconclusive(repo, monkeypatch):
    # The package lives in lib/, which the gate does not put on sys.path, and
    # PYTHONPATH points at the real checkout (which is at head). This is what
    # an editable install does: the base run would silently test head code.
    base = repo.commit({"lib/mypkg/__init__.py": "def double(x):\n    return x + x + 1\n"})
    head = repo.commit({"lib/mypkg/__init__.py": "def double(x):\n    return x + x\n",
                        "tests/test_pkg.py": "import mypkg\n\ndef test_d():\n    assert mypkg.double(2) == 4\n"})
    monkeypatch.setenv("PYTHONPATH", str(repo.path / "lib"))
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.verdict == INCONCLUSIVE
    assert "outside the base worktree" in test.reason and "mypkg" in test.reason
    assert any("outside the head worktree" in w for w in result.warnings)


def test_action_style_shallow_merge_commit(repo, tmp_path):
    # actions/checkout on pull_request gives a merge commit with fetch-depth 2.
    # The action passes HEAD^1 (base branch) and HEAD (the merge) to the CLI.
    repo.commit(SRC_BASE)
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit({**SRC_HEAD, **STRONG_TEST})
    repo.git("checkout", "-q", "main")
    repo.commit({"README": "main moved on\n"})
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "merge")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "2", f"file://{repo.path}", str(clone)], check=True)
    result = run_gate(clone, "HEAD^1", "HEAD", python=sys.executable)
    assert result.test_files == ["tests/test_half.py"]
    assert [t.verdict for t in result.tests] == [STRONG]
