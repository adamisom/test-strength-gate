"""End-to-end edge cases through the CLI and the gate."""

import functools
import importlib.util
import json
import os
import subprocess
import sys
import zipfile

import pytest

from helpers import GitRepo
from test_strength_gate.classify import BROKEN_AT_HEAD, INCONCLUSIVE, STRONG, WEAK
from test_strength_gate import gate, runner
from test_strength_gate.cli import main
from test_strength_gate.gate import run_gate
from test_strength_gate.report import to_markdown

needs_xdist = pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="needs pytest-xdist")

SRC_BASE = {"lib.py": "def double(x):\n    return x + x\n\ndef half(x):\n    return x // 2\n"}
SRC_HEAD = {"lib.py": "def double(x):\n    return 2 * x\n\ndef half(x):\n    return x / 2\n"}
WEAK_TEST = {"tests/test_lib.py": "import lib\n\ndef test_double():\n    assert lib.double(3) == 6\n"}
STRONG_TEST = {"tests/test_half.py": "import lib\n\ndef test_half():\n    assert lib.half(3) == 1.5\n"}
# A PR that refactors double() and adds a data file its new test reads. The
# source change can't be caught, so the test must never come out strong.
DATA_TEST = {"tests/data/expected.json": '{"x": 3, "want": 6}\n',
             "tests/test_data.py": "import json, pathlib, lib\n\ndef test_from_file():\n"
             "    case = json.loads((pathlib.Path(__file__).parent / 'data' / 'expected.json').read_text())\n"
             "    assert lib.double(case['x']) == case['want']\n"}
REFACTOR = {"lib.py": "def double(x):\n    return 2 * x\n\ndef half(x):\n    return x // 2\n"}


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


def test_a_deleted_test_file_has_no_tests_to_judge(repo):
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


def test_generated_version_file_is_copied_into_worktrees(repo):
    # hatch-vcs and setuptools-scm write mypkg/version.py into the checkout at
    # install time, and git ignores it, so a bare worktree can't import mypkg.
    base = repo.commit({".gitignore": "mypkg/version.py\n.venv/\n",
                        "mypkg/__init__.py": "from .version import v\n\ndef double(x):\n    return x + x + 1\n"})
    head = repo.commit({"mypkg/__init__.py": "from .version import v\n\ndef double(x):\n    return x + x\n",
                        "tests/test_pkg.py": "import mypkg\n\ndef test_d():\n    assert mypkg.double(2) == 4\n"})
    repo.write({"mypkg/version.py": "v = '1.0'\n", ".venv/lib/site.py": "x = 1\n"})
    result = repo.gate(base, head)
    assert [t.verdict for t in result.tests] == [STRONG]
    assert any("mypkg/version.py" in w and ".venv" not in w for w in result.warnings)


def test_generated_modules_in_an_ignored_folder_are_copied_into_worktrees(repo):
    # Fable audit 2, TSG-19. Code generators such as protobuf write a whole
    # folder, and .gitignore names the folder. --directory hid its files, so
    # the package failed to import and the test was BROKEN_AT_HEAD. A real
    # virtualenv in another ignored folder must still be left out.
    base = repo.commit({".gitignore": "pkg/gen/\nenvs/\n",
                        "pkg/__init__.py": "from .gen.api_pb2 import VERSION\n\ndef half(x):\n    return x // 2\n"})
    head = repo.commit({"pkg/__init__.py": "from .gen.api_pb2 import VERSION\n\ndef half(x):\n    return x / 2\n",
                        "tests/test_pkg.py": "import pkg\n\ndef test_half():\n    assert pkg.half(3) == 1.5\n"})
    repo.write({"pkg/gen/__init__.py": "", "pkg/gen/api_pb2.py": "VERSION = 1\n",
                "envs/py/pyvenv.cfg": "home = /usr\n", "envs/py/lib/site.py": "x = 1\n"})
    result = repo.gate(base, head)
    assert [t.verdict for t in result.tests] == [STRONG]
    [warning] = [w for w in result.warnings if "Copied git-ignored" in w]
    assert "pkg/gen/api_pb2.py" in warning and "envs/" not in warning


def test_submodule_contents_reach_both_runs(repo, tmp_path):
    # Fable audit 2, TSG-20. git worktree add leaves a submodule's folder
    # empty, so a test that reads data from it failed in both runs and came
    # out BROKEN_AT_HEAD. The gate fills it from the checkout's clone, and
    # says so when it can't.
    data = GitRepo(tmp_path / "data")
    data.commit({"n.txt": "3\n"})
    repo.commit(SRC_BASE)
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(data.path), "tests/data")
    base = repo.commit({})
    head = repo.commit({**SRC_HEAD, "tests/test_lib.py": "import lib\nfrom pathlib import Path\n\n"
                        "def test_half():\n    n = int((Path(__file__).parent / 'data' / 'n.txt').read_text())\n"
                        "    assert lib.half(n) == 1.5\n"})
    result = repo.gate(base, head)
    assert [t.verdict for t in result.tests] == [STRONG]
    assert not any("Submodules" in w for w in result.warnings)

    repo.git("submodule", "deinit", "-q", "-f", "tests/data")
    result = repo.gate(base, head)
    assert any(w.startswith("Submodules") and "tests/data" in w for w in result.warnings)


def test_a_pass_at_base_that_imported_a_copied_generated_module_is_inconclusive(repo):
    # Codex TSG-15: the checkout's ignored version.py was generated for head,
    # and the gate copies it into the base worktree too. A test of the value
    # it holds then passes at base, which says nothing about the base code,
    # so the verdict is inconclusive, and the reason and a note at the top
    # name the copied module (DECISIONS 50).
    base = repo.commit({".gitignore": "pkg/version.py\n", "pyproject.toml": "version = '1.0'\n",
                        "pkg/__init__.py": "",
                        "pkg/app.py": "from .version import VERSION\n\ndef current():\n    return VERSION\n"})
    head = repo.commit({"pyproject.toml": "version = '2.0'\n",
                        "tests/test_app.py": "from pkg.app import current\n\n"
                                             "def test_version():\n    assert current() == '2.0'\n"})
    repo.write({"pkg/version.py": "VERSION = '2.0'\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.base.status == "passed"
    assert test.verdict == INCONCLUSIVE
    assert "pkg/version.py" in test.reason and "generated for head" in test.reason
    assert result.copied_imported == ["pkg/version.py"]
    assert "pkg/version.py" in to_markdown(result).split("| Test |")[0]


def test_a_copied_file_read_by_exec_in_its_package_counts_as_imported(repo):
    # Fable audit 2, TSG-24. The package reads its generated _version.py with
    # exec() instead of importing it, so no module was loaded from the file,
    # and the pass at base came out weak with no caveat.
    init = ("from pathlib import Path\n\nns = {}\n"
            "exec((Path(__file__).parent / '_version.py').read_text(), ns)\nVERSION = ns['VERSION']\n")
    base = repo.commit({".gitignore": "pkg/_version.py\n", "pkg/__init__.py": init,
                        "pkg/app.py": "def f():\n    return 1\n"})
    head = repo.commit({"tests/test_app.py": "import pkg\n\ndef test_version():\n"
                                             "    assert pkg.VERSION == '2.0'\n"})
    repo.write({"pkg/_version.py": "VERSION = '2.0'\n"})
    result = repo.gate(base, head)
    assert result.copied_imported == ["pkg/_version.py"]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_a_module_that_only_mentions_the_copied_files_stem_does_not_count(repo):
    # Fable audit 3, TSG-37. The sibling rule matched "version" as a word, so a
    # cli.py with a --version option counted as reading the copied version.py.
    base = repo.commit({".gitignore": "pkg/version.py\n", "pkg/__init__.py": "",
                        "pkg/cli.py": "OPTIONS = ['--version']\n\ndef f():\n    return 1\n"})
    head = repo.commit({"tests/test_cli.py": "from pkg.cli import f\n\ndef test_f():\n    assert f() == 1\n"})
    repo.write({"pkg/version.py": "VERSION = '2.0'\n"})
    result = repo.gate(base, head)
    assert result.copied_imported == []
    assert [t.verdict for t in result.tests] == [WEAK]


@needs_xdist
def test_a_copied_module_imported_in_an_xdist_worker_is_seen(repo):
    # Fable audit 3, TSG-42: the copied-file rule had no xdist test.
    base = repo.commit({".gitignore": "pkg/version.py\n", "pytest.ini": "[pytest]\naddopts = -n 2\n",
                        "pkg/__init__.py": "",
                        "pkg/app.py": "from .version import VERSION\n\ndef current():\n    return VERSION\n"})
    head = repo.commit({"tests/test_app.py": "from pkg.app import current\n\n"
                                             "def test_version():\n    assert current() == '2.0'\n"})
    repo.write({"pkg/version.py": "VERSION = '2.0'\n"})
    result = repo.gate(base, head)
    assert result.copied_imported == ["pkg/version.py"]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_a_broken_ignored_symlink_and_a_conda_env_are_not_copied(repo):
    # Fable audit 3, TSG-38 and TSG-39. A git-ignored .py symlink with no
    # target crashed the copy, and a conda env, which has no pyvenv.cfg, had
    # its whole standard library copied.
    base = repo.commit({**SRC_BASE, ".gitignore": "gone.py\nenvs/\n"})
    head = repo.commit({**SRC_HEAD, **STRONG_TEST})
    (repo.path / "gone.py").symlink_to(repo.path / "missing.py")
    repo.write({"envs/py/conda-meta/history": "", "envs/py/lib/python3.12/os.py": "x = 1\n"})
    result = repo.gate(base, head)
    assert [t.verdict for t in result.tests] == [STRONG]
    assert not any("Copied git-ignored" in w for w in result.warnings)


def test_a_copied_module_the_base_run_never_imported_adds_no_caveat(repo):
    base = repo.commit({".gitignore": "pkg/version.py\n", "pkg/__init__.py": "",
                        "pkg/app.py": "def f():\n    return 1\n"})
    head = repo.commit({"tests/test_app.py": "from pkg.app import f\n\ndef test_f():\n    assert f() == 1\n"})
    repo.write({"pkg/version.py": "VERSION = '2.0'\n"})
    result = repo.gate(base, head)
    assert [t.verdict for t in result.tests] == [WEAK]
    assert result.copied_imported == []
    assert "generated for head" not in result.tests[0].reason


def _fake_head_install(tmp_path, monkeypatch, folder="metapkg-2.0.dist-info", name="metapkg"):
    # A site folder with head's .dist-info stands in for the action's install.
    info = tmp_path / "site" / folder
    info.mkdir(parents=True)
    text = f"Metadata-Version: 2.1\nName: {name}\nVersion: 2.0\n"
    (info / ("METADATA" if folder.endswith(".dist-info") else "PKG-INFO")).write_text(text)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site"))


def _version_pr(repo, name="metapkg", reader="importlib.metadata", pyproject="pyproject.toml", extra=None,
                globs=None):
    """A PR that bumps the version in `pyproject` and tests the installed version."""
    base = repo.commit({pyproject: f"[project]\nname = '{name}'\nversion = '1.0'\n",
                        "metapkg/__init__.py": f"from {reader} import version\n\n"
                                               f"__version__ = version('{name}')\n", **(extra or {})})
    head = repo.commit({pyproject: f"[project]\nname = '{name}'\nversion = '2.0'\n",
                        "tests/test_version.py": "import metapkg\n\ndef test_version():\n"
                                                 "    assert metapkg.__version__ == '2.0'\n"})
    return repo.gate(base, head, globs=globs)


@pytest.mark.parametrize("folder, name", [
    ("my_project-2.0.dist-info", "my-project"),  # what pip and uv write (Codex TSG-29, rejected)
    ("my_project.egg-info", "my-project"),       # setuptools' egg_info, with no version (TSG-30)
    ("My_Project.egg-info", "My-Project"),
    ("foo_bar-2.0.dist-info", "foo.bar"),        # dots normalize too (Fable audit 3, TSG-42)
])
def test_metadata_reads_are_seen_for_every_folder_name(repo, tmp_path, monkeypatch, folder, name):
    _fake_head_install(tmp_path, monkeypatch, folder, name)
    result = _version_pr(repo, name)
    assert result.metadata_read == [name.lower().replace(".", "-")]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_a_metadata_read_while_pytest_imports_a_conftest_is_seen(repo, tmp_path, monkeypatch):
    # Fable audit 3, TSG-35. The wrapper went in at session start, after
    # pytest imported tests/conftest.py, whose import of the package read the
    # version, so the read was missed and the test stayed weak.
    _fake_head_install(tmp_path, monkeypatch)
    result = _version_pr(repo, extra={"tests/conftest.py": "import metapkg\n"})
    assert result.metadata_read == ["metapkg"]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_pytests_own_reads_of_a_plugin_projects_metadata_do_not_count(repo, tmp_path, monkeypatch):
    # Fable audit 3, TSG-36. The project is a pytest plugin, and with -v
    # pytest's session header reads its metadata to list it. No test reads
    # the metadata, so the verdict stays weak.
    _fake_head_install(tmp_path, monkeypatch)
    info = tmp_path / "site" / "metapkg-2.0.dist-info"
    (info / "entry_points.txt").write_text("[pytest11]\nmetapkg = metapkg.plugin\n")
    base = repo.commit({"pytest.ini": "[pytest]\naddopts = -v\n",
                        "pyproject.toml": "[project]\nname = 'metapkg'\ndependencies = []\n",
                        "metapkg/__init__.py": "",
                        "metapkg/plugin.py": "import pytest\n\n@pytest.fixture\ndef answer():\n    return 42\n"})
    head = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\ndependencies = ['attrs']\n",
                        "tests/test_answer.py": "def test_answer(answer):\n    assert answer == 42\n"})
    result = repo.gate(base, head)
    assert result.metadata_read == []
    assert [t.verdict for t in result.tests] == [WEAK]


def test_a_zipped_distribution_on_the_path_does_not_break_the_base_run(repo, tmp_path, monkeypatch):
    # Fable audit 3, TSG-34. importlib.metadata gives a distribution inside a
    # zip a zipfile.Path, which the wrapper couldn't turn into a Path, so
    # entry_points() raised a TypeError at base only and the test was strong.
    _fake_head_install(tmp_path, monkeypatch)
    (tmp_path / "site" / "metapkg-2.0.dist-info" / "entry_points.txt").write_text(
        "[metaplugins]\nnew = metapkg:f\n")
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("zdist-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: zdist\nVersion: 1.0\n")
        z.writestr("zdist-1.0.dist-info/entry_points.txt", "[other]\nx = y:z\n")
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path / 'site'}{os.pathsep}{bundle}")
    base = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\n",
                        "metapkg/__init__.py": "from importlib.metadata import entry_points\n\n"
                                               "def f():\n    pass\n\n"
                                               "def plugins():\n    return sorted(e.name for e in "
                                               "entry_points(group='metaplugins'))\n"})
    head = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\n\n"
                                          "[project.entry-points.metaplugins]\nnew = 'metapkg:f'\n",
                        "tests/test_plugins.py": "import metapkg\n\ndef test_plugins():\n"
                                                 "    assert metapkg.plugins() == ['new']\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.base.status == "passed"
    assert (test.verdict, result.metadata_read) == (INCONCLUSIVE, ["metapkg"])


@pytest.mark.parametrize("pyproject, globs", [
    ("pyproject.toml", ["tests/**", "pyproject.toml"]),
    ("packages/metapkg/pyproject.toml", ["tests/**", "packages/**"]),
])
def test_a_packaging_file_the_test_globs_name_is_still_a_packaging_change(repo, tmp_path, monkeypatch,
                                                                           pyproject, globs):
    # Codex round 3, TSG-44. TSG-40 left out packaging files that match the
    # test globs, so a glob naming the real pyproject.toml hid the change and
    # the version test came out a silent weak.
    _fake_head_install(tmp_path, monkeypatch)
    result = _version_pr(repo, pyproject=pyproject, globs=globs)
    assert (result.packaging_files, result.metadata_read) == ([pyproject], ["metapkg"])
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_a_pyproject_that_is_test_data_is_not_a_packaging_change(repo):
    # Fable audit 3, TSG-40.
    base = repo.commit(SRC_BASE)
    head = repo.commit({**WEAK_TEST, "tests/fixtures/pyproject.toml": "[project]\nname = 'sample'\n",
                        "pkg/tests/fixtures/setup.cfg": "[metadata]\nname = sample\n"})
    result = repo.gate(base, head)
    assert result.packaging_files == []
    assert "installed metadata" not in result.tests[0].reason


@pytest.mark.parametrize("files, expected", [
    ({"setup.py": "from setuptools import setup\n\nsetup(name='from-setup-py', version='1')\n"},
     ["from-setup-py"]),
    ({"setup.cfg": "[metadata]\nname = from-setup-cfg\n"}, ["from-setup-cfg"]),
    ({"pyproject.toml": "[tool.poetry]\nname = \"from-poetry\"\n\n[tool.poetry.dependencies]\n"
                        "name = \"not-this\"\n"}, ["from-poetry"]),
])
def test_distribution_names_come_from_each_packaging_file(repo, files, expected):
    # Fable audit 3, TSG-42: no test read names from setup.py, setup.cfg or Poetry.
    head = repo.commit(files)
    assert gate._distribution_names(repo.path, head) == expected


def test_a_changed_pyproject_below_the_root_names_its_distribution(repo, tmp_path, monkeypatch):
    # TSG-31: in a monorepo the changed pyproject.toml isn't at the root.
    _fake_head_install(tmp_path, monkeypatch)
    result = _version_pr(repo, pyproject="packages/metapkg/pyproject.toml")
    assert result.metadata_read == ["metapkg"]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


@pytest.mark.skipif(importlib.util.find_spec("importlib_metadata") is None, reason="needs importlib_metadata")
def test_a_metadata_read_through_the_backport_is_seen(repo, tmp_path, monkeypatch):
    # TSG-32: the importlib_metadata backport has its own PathDistribution.
    _fake_head_install(tmp_path, monkeypatch)
    result = _version_pr(repo, reader="importlib_metadata")
    assert result.metadata_read == ["metapkg"]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_a_pass_at_base_that_read_heads_installed_metadata_is_inconclusive(repo, tmp_path, monkeypatch):
    # Fable audit 2, TSG-18. importlib.metadata reads the installed package's
    # .dist-info, which head's install wrote, so a test of a bumped version
    # passes at base. That pass says nothing about the base code (DECISIONS 62).
    _fake_head_install(tmp_path, monkeypatch)
    base = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\nversion = '1.0'\n",
                        "metapkg/__init__.py": "from importlib.metadata import version\n\n"
                                               "__version__ = version('metapkg')\n"})
    head = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\nversion = '2.0'\n",
                        "tests/test_version.py": "import metapkg\n\ndef test_version():\n"
                                                 "    assert metapkg.__version__ == '2.0'\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert (test.base.status, test.verdict) == ("passed", INCONCLUSIVE)
    assert (result.packaging_files, result.metadata_read) == (["pyproject.toml"], ["metapkg"])
    assert "metapkg" in test.reason and "head's install" in test.reason
    assert "metadata from head's install" in to_markdown(result).split("| Test |")[0]


@needs_xdist
def test_a_metadata_read_in_an_xdist_worker_is_seen(repo, tmp_path, monkeypatch):
    _fake_head_install(tmp_path, monkeypatch)
    base = repo.commit({"pytest.ini": "[pytest]\naddopts = -n 2\n",
                        "pyproject.toml": "[project]\nname = 'metapkg'\nversion = '1.0'\n",
                        "metapkg/__init__.py": "from importlib.metadata import version\n\n"
                                               "def current():\n    return version('metapkg')\n"})
    head = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\nversion = '2.0'\n",
                        "tests/test_version.py": "import metapkg\n\ndef test_version():\n"
                                                 "    assert metapkg.current() == '2.0'\n"})
    result = repo.gate(base, head)
    assert result.metadata_read == ["metapkg"]
    assert [t.verdict for t in result.tests] == [INCONCLUSIVE]


def test_a_packaging_change_whose_metadata_the_base_run_never_read_stays_weak(repo, tmp_path, monkeypatch):
    # The PR changes pyproject.toml, but no test reads the installed metadata,
    # so the verdict stays weak, with the caveat in case it was read some
    # way the plugin can't see, such as pkg_resources.
    _fake_head_install(tmp_path, monkeypatch)
    base = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\ndependencies = []\n",
                        "metapkg/__init__.py": "def f():\n    return 1\n"})
    head = repo.commit({"pyproject.toml": "[project]\nname = 'metapkg'\ndependencies = ['attrs']\n",
                        "tests/test_f.py": "import metapkg\n\ndef test_f():\n    assert metapkg.f() == 1\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.verdict == WEAK and result.metadata_read == []
    assert "pyproject.toml" in test.reason and "head's install" in test.reason
    assert "installed metadata" in to_markdown(result).split("| Test |")[0]


def test_non_python_fixtures_are_copied_but_not_collected(repo):
    # A PR adds a data file under tests/ that its new test reads. With a glob
    # that matches it, the file travels to base but is not passed to pytest.
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, "tests/data/test_case.robot": "3\n",
                        "tests/test_data.py": "import lib, pathlib\n\ndef test_half_from_file():\n"
                        "    n = int((pathlib.Path(__file__).parent / 'data' / 'test_case.robot').read_text())\n"
                        "    assert lib.half(n) == 1.5\n"})
    result = repo.gate(base, head, globs=["tests/**"])
    assert sorted(result.test_files) == ["tests/data/test_case.robot", "tests/test_data.py"]
    assert [t.verdict for t in result.tests] == [STRONG]
    assert result.warnings == []


def test_new_data_file_under_tests_travels_with_the_default_globs(repo):
    # Codex review finding 1. The default patterns include tests/**, so the
    # data file reaches base, and the test is judged on the refactor alone.
    base = repo.commit(SRC_BASE)
    head = repo.commit({**REFACTOR, **DATA_TEST})
    result = repo.gate(base, head)
    assert sorted(result.test_files) == ["tests/data/expected.json", "tests/test_data.py"]
    assert [t.verdict for t in result.tests] == [WEAK]


def test_file_missing_at_base_but_present_at_head_is_inconclusive(repo):
    # Codex review finding 1. With patterns that leave the data file behind,
    # base raises FileNotFoundError. That says nothing about the source change.
    base = repo.commit(SRC_BASE)
    head = repo.commit({**REFACTOR, **DATA_TEST})
    [test] = repo.gate(base, head, globs=["tests/**/*.py"]).tests
    assert test.verdict == INCONCLUSIVE
    assert "tests/data/expected.json" in test.reason and "exists at head" in test.reason


def test_binary_file_missing_at_base_but_present_at_head_is_inconclusive(repo):
    # Codex re-review, remaining 1. The head-existence check must not decode
    # the file: a binary fixture used to crash the gate with UnicodeDecodeError.
    base = repo.commit(SRC_BASE)
    (repo.path / "tests" / "data").mkdir(parents=True)
    (repo.path / "tests" / "data" / "blob.bin").write_bytes(b"\xff\xfe\x00\x80\x81binary")
    head = repo.commit({**REFACTOR, "tests/test_blob.py": "import pathlib, lib\n\ndef test_blob():\n"
                        "    data = (pathlib.Path(__file__).parent / 'data' / 'blob.bin').read_bytes()\n"
                        "    assert lib.double(len(data)) == 2 * len(data)\n"})
    [test] = repo.gate(base, head, globs=["tests/**/*.py"]).tests
    assert test.verdict == INCONCLUSIVE
    assert "tests/data/blob.bin" in test.reason and "exists at head" in test.reason


def test_file_missing_at_both_base_and_head_commits_stays_strong(repo):
    # A FileNotFoundError for a file the head commit doesn't have either is
    # behavior: here the old code forgets to create the output file.
    base = repo.commit({"lib.py": "def save(path):\n    pass\n"})
    head = repo.commit({"lib.py": "def save(path):\n    open(path, 'w').write('ok')\n",
                        "tests/test_save.py": "import lib\n\ndef test_save(tmp_path):\n"
                        "    lib.save(tmp_path / 'out.txt')\n"
                        "    assert (tmp_path / 'out.txt').read_text() == 'ok'\n"})
    [test] = repo.gate(base, head).tests
    assert (test.verdict, test.base.exc_type) == (STRONG, "FileNotFoundError")


def test_non_assertion_strong_says_where_it_was_raised(repo):
    # Codex review finding 2. A strong verdict from an exception other than an
    # assertion names the line that raised it, so a reviewer can tell an error
    # in the old code from one in the test's own code or in a library.
    base = repo.commit({"lib.py": "import json\n\ndef parse(text):\n    return json.loads(text)\n\n"
                                  "def check(n):\n    return n\n"})
    head = repo.commit({"lib.py": "import json\n\ndef parse(text):\n    return json.loads(text or 'null')\n\n"
                                  "def check(n):\n    if n < 0:\n        return 0\n    return n\n",
                        "tests/test_lib.py": "import lib\n\ndef test_parse_empty():\n    assert lib.parse('') is None\n\n"
                        "def test_check():\n    [1, 2][lib.check(-5)]\n"})
    by_name = {t.id.split("::")[1]: t for t in repo.gate(base, head).tests}
    parse, check = by_name["test_parse_empty"], by_name["test_check"]
    assert (parse.verdict, check.verdict) == (STRONG, STRONG)
    assert "raised in library code called from lib.py:4" in parse.reason
    assert "raised at tests/test_lib.py:7" in check.reason


def test_changed_data_file_outside_the_patterns_is_named_at_the_top_of_the_report(repo, tmp_path):
    # Codex re-review, remaining 3. The PR only refactors total(), but it also
    # renames a key in a data file outside tests/. The base run reads the old
    # file, fails with KeyError, and the test looks strong. No exception rule
    # can see this, so the report must name the file where a reviewer looks.
    base = repo.commit({"lib.py": "def total(xs):\n    return sum(xs)\n", "data/cases.json": '{"items": [1, 2]}\n',
                        "docs/guide.md": "old\n"})
    head = repo.commit({"lib.py": "def total(xs):\n    return sum(x for x in xs)\n",
                        "data/cases.json": '{"values": [1, 2]}\n', "docs/guide.md": "new\n",
                        "tests/test_total.py": "import json, lib\n\ndef test_total():\n"
                        "    case = json.load(open('data/cases.json'))\n    assert lib.total(case['values']) == 3\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.verdict == STRONG and "inspect the cause" in test.reason
    assert result.other_files == ["data/cases.json"]  # docs are left out
    out = tmp_path / "r.md"
    cli(repo, base, head, "--markdown", str(out))
    top = out.read_text().split("| Test |")[0]
    assert "`data/cases.json`" in top and "old version" in top


def test_argument_type_error_inside_the_old_code_says_the_cause_is_unclear(repo):
    # Codex review finding 3. The old total() calls _sum() without an argument
    # it needs. That is a bug the test catches, not a new API, but Python's
    # message looks the same. It stays inconclusive, with an honest reason.
    base = repo.commit({"lib.py": "def _sum(items, start):\n    return sum(items, start)\n\n"
                                  "def total(items):\n    return _sum(items)\n"})
    head = repo.commit({"lib.py": "def _sum(items, start):\n    return sum(items, start)\n\n"
                                  "def total(items):\n    return _sum(items, 0)\n",
                        "tests/test_total.py": "import lib\n\ndef test_total():\n    assert lib.total([1, 2]) == 3\n"})
    [test] = repo.gate(base, head).tests
    assert (test.verdict, test.base.origin) == (INCONCLUSIVE, "project")
    assert "inside the base code at lib.py:5" in test.reason
    assert "usually means the new API" not in test.reason


def test_a_model_field_that_is_new_at_head_is_inconclusive(repo):
    # Fable audit 2, TSG-17. Django rejects an unknown field in its own words,
    # "Author() got unexpected keyword arguments: 'nickname'", which the
    # anchored pattern missed, so a test of a new field came out strong.
    model = ("class Author:\n    fields = {fields}\n\n    def __init__(self, **kwargs):\n"
             "        unexpected = [k for k in kwargs if k not in self.fields]\n"
             "        if unexpected:\n"
             "            raise TypeError(f\"{{type(self).__name__}}() got unexpected keyword arguments: \"\n"
             "                            + ', '.join(repr(k) for k in unexpected))\n"
             "        self.__dict__.update(kwargs)\n")
    base = repo.commit({"models.py": model.format(fields="('name',)")})
    head = repo.commit({"models.py": model.format(fields="('name', 'nickname')"),
                        "tests/test_author.py": "from models import Author\n\ndef test_nickname():\n"
                                                "    assert Author(name='a', nickname='b').nickname == 'b'\n"})
    [test] = repo.gate(base, head).tests
    assert test.base.message == "Author() got unexpected keyword arguments: 'nickname'"
    assert test.verdict == INCONCLUSIVE


def test_bare_assertion_reason_shows_the_line_and_the_stderr_error(repo):
    # Codex review finding 4, the robocop case in small: the check lives in a
    # helper pytest doesn't rewrite, so its AssertionError has no message, and
    # the real cause is the old CLI rejecting the new option on stderr.
    cli_base = ("import sys\n\ndef main(args):\n    for a in args:\n        if a != '--x':\n"
                "            print(f'error: unknown option {a}', file=sys.stderr)\n            return 2\n    return 0\n")
    base = repo.commit({"cli.py": cli_base, "support/__init__.py": "",
                        "support/check.py": "def expect_code(got, want):\n    assert got == want\n"})
    head = repo.commit({"cli.py": cli_base.replace("if a != '--x'", "if a not in ('--x', '--new')"),
                        "tests/test_cli.py": "import cli\nfrom support.check import expect_code\n\n"
                        "def test_new_option():\n    expect_code(cli.main(['--new']), 0)\n"})
    [test] = repo.gate(base, head).tests
    assert test.verdict == STRONG
    assert "no message, at support/check.py:2: `assert got == want`" in test.reason
    assert "Last error on stderr: error: unknown option --new" in test.reason


def test_installed_copy_of_a_lib_layout_package_makes_base_inconclusive(repo, tmp_path, monkeypatch):
    # Codex review finding 5. The package lives in lib/, and a regular (not
    # editable) install put a copy of the head version in site-packages, far
    # from the checkout. The base run imports that copy and would call the
    # test weak. The plugin must see that mypkg is the project's own package.
    base = repo.commit({"lib/mypkg/__init__.py": "def double(x):\n    return x + x + 1\n"})
    head = repo.commit({"lib/mypkg/__init__.py": "def double(x):\n    return x + x\n",
                        "tests/test_pkg.py": "import mypkg\n\ndef test_d():\n    assert mypkg.double(2) == 4\n"})
    site = tmp_path / "site-packages"
    (site / "mypkg").mkdir(parents=True)
    (site / "mypkg" / "__init__.py").write_text("def double(x):\n    return x + x\n")
    monkeypatch.setenv("PYTHONPATH", str(site))
    [test] = repo.gate(base, head).tests
    assert test.verdict == INCONCLUSIVE
    assert "outside the base worktree" in test.reason and "mypkg" in test.reason


def test_new_class_decorator_makes_an_unchanged_method_modified(repo):
    # Codex review finding 6. The PR adds @pytest.mark.usefixtures to a test
    # class and leaves the method alone. Its setup changed, so it is judged.
    fixture = ("import pytest\n\n@pytest.fixture\ndef fast_lib(monkeypatch):\n    import lib\n"
               "    monkeypatch.setattr(lib, 'double', lambda x: 2 * x)\n")
    method = "    def test_d(self):\n        assert lib.double(2) == 4\n"
    base = repo.commit({"lib.py": "def double(x):\n    return x + x + 1\n", "tests/conftest.py": fixture,
                        "tests/test_cls.py": "import lib\n\nclass TestD:\n" + method})
    head = repo.commit({"lib.py": "def double(x):\n    return x + x\n",
                        "tests/test_cls.py": "import lib, pytest\n\n@pytest.mark.usefixtures('fast_lib')\n"
                                             "class TestD:\n" + method})
    result = repo.gate(base, head)
    assert [(t.id, t.kind) for t in result.tests] == [("tests/test_cls.py::TestD::test_d", "modified")]


def test_test_file_that_fails_to_import_at_head_gets_its_own_row(repo, tmp_path):
    # Codex review finding 7. A new test file with a syntax error has no test
    # IDs, so it used to vanish, and the report said "No added or modified
    # tests to judge". It now has a BROKEN_AT_HEAD row and its own count.
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, **STRONG_TEST,
                        "tests/test_broken.py": "import lib\n\ndef test_b(:\n    assert lib.double(2) == 4\n"})
    result = repo.gate(base, head)
    rows = {t.id: t for t in result.tests}
    assert rows["tests/test_broken.py"].verdict == BROKEN_AT_HEAD
    assert "fails to import at head" in rows["tests/test_broken.py"].reason
    assert rows["tests/test_half.py::test_half"].verdict == STRONG
    out = tmp_path / "r.md"
    cli(repo, base, head, "--markdown", str(out))
    assert ("1 new test: 1 strong, 0 weak (pass without the source change), 0 inconclusive. "
            "Also, 1 changed test file fails to import at head, so its tests were not judged.") in out.read_text()


def test_head_timeout_is_an_inconclusive_file_row_not_broken(repo, monkeypatch):
    # Codex re-review, remaining 2. A valid test file whose head run times out
    # was reported as BROKEN_AT_HEAD, although its head result is unknown. It
    # now gets an INCONCLUSIVE file row that gives the reason.
    monkeypatch.setattr(gate, "run_pytest", functools.partial(runner.run_pytest, timeout=3))
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, "tests/test_slow.py": "import time\nimport lib\n\ntime.sleep(60)\n\n"
                        "def test_half():\n    assert lib.half(3) == 1.5\n"})
    result = repo.gate(base, head)
    [row] = result.tests
    assert (row.id, row.verdict) == ("tests/test_slow.py", INCONCLUSIVE)
    assert "pytest timed out after 3 seconds" in row.reason


def test_fixture_deleted_under_the_patterns_is_deleted_at_base_too(repo):
    # Codex re-review, round 2. The PR deletes a data file under tests/ and
    # adds a test that checks it is gone. The base run must see the PR's test
    # side, deletions included, or the test fails at base on the stale file
    # and looks strong. Nothing in the source changed, so it is weak.
    base = repo.commit({**SRC_BASE, "tests/data/old.txt": "stale\n"})
    head = repo.commit({**REFACTOR, "tests/data/old.txt": None,
                        "tests/test_cleanup.py": "import pathlib\n\ndef test_old_fixture_is_gone():\n"
                        "    assert not (pathlib.Path(__file__).parent / 'data' / 'old.txt').exists()\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.verdict == WEAK
    assert result.deleted_files == ["tests/data/old.txt"]


def test_deleted_file_outside_the_patterns_is_named_at_the_top_of_the_report(repo, tmp_path):
    # Codex re-review, round 2. A file the PR deletes outside the patterns is
    # still present in the base run, so the report must name it like a
    # changed one, and say that it was deleted.
    base = repo.commit({**SRC_BASE, "data/old.txt": "stale\n", "docs/old.md": "old\n"})
    head = repo.commit({**REFACTOR, "data/old.txt": None, "docs/old.md": None,
                        "tests/test_cleanup.py": "import pathlib\n\ndef test_old_data_is_gone():\n"
                        "    assert not pathlib.Path('data/old.txt').exists()\n"})
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.verdict == STRONG  # a false strong that only the note can flag
    assert result.other_files == ["data/old.txt"]  # docs are left out
    out = tmp_path / "r.md"
    cli(repo, base, head, "--markdown", str(out))
    top = out.read_text().split("| Test |")[0]
    assert "`data/old.txt` (deleted)" in top and "still had it" in top


def test_test_side_file_replaced_by_a_folder_of_the_same_name(repo):
    # The PR deletes tests/data and adds tests/data/x.txt. The deletion must
    # happen before the copy, or git refuses to remove what is by then a folder.
    base = repo.commit({**SRC_BASE, "tests/data": "old\n"})
    head = repo.commit({**REFACTOR, "tests/data": None, "tests/data/x.txt": "new\n",
                        "tests/test_x.py": "import pathlib\n\ndef test_x():\n"
                        "    assert (pathlib.Path(__file__).parent / 'data' / 'x.txt').read_text() == 'new\\n'\n"})
    [test] = repo.gate(base, head).tests
    assert test.verdict == WEAK


XDIST = {"pytest.ini": "[pytest]\naddopts = -n 2\n"}


@needs_xdist
def test_xdist_in_addopts_gives_the_usual_verdicts(repo):
    base = repo.commit({**SRC_BASE, **XDIST})
    head = repo.commit({**SRC_HEAD, **WEAK_TEST, **STRONG_TEST})
    result = repo.gate(base, head)
    assert {t.id: t.verdict for t in result.tests} == {
        "tests/test_lib.py::test_double": WEAK, "tests/test_half.py::test_half": STRONG}


@needs_xdist
def test_installed_copy_under_xdist_makes_base_inconclusive(repo, tmp_path, monkeypatch):
    # Fable audit TSG-2. Under pytest-xdist the workers import the project,
    # so the check for code loaded from outside the worktree has to run there
    # too. Before, it ran only in the main process and the test came out weak.
    base = repo.commit({"lib/mypkg/__init__.py": "def double(x):\n    return x + x + 1\n", **XDIST})
    head = repo.commit({"lib/mypkg/__init__.py": "def double(x):\n    return x + x\n",
                        "tests/test_pkg.py": "import mypkg\n\ndef test_d():\n    assert mypkg.double(2) == 4\n"})
    site = tmp_path / "site-packages"
    (site / "mypkg").mkdir(parents=True)
    (site / "mypkg" / "__init__.py").write_text("def double(x):\n    return x + x\n")
    monkeypatch.setenv("PYTHONPATH", str(site))
    result = repo.gate(base, head)
    [test] = result.tests
    assert test.verdict == INCONCLUSIVE
    assert "outside the base worktree" in test.reason and "mypkg" in test.reason
    assert any("outside the head worktree" in w for w in result.warnings)


def test_x_in_addopts_does_not_stop_the_judged_runs(repo):
    # Fable audit TSG-3. With -x in addopts, the first failure stopped each
    # run: at base the later tests were "not collected", and at head they
    # were BROKEN_AT_HEAD. The runner now passes --maxfail=0 after them.
    x = {"pytest.ini": "[pytest]\naddopts = -x\n"}
    base = repo.commit({**SRC_BASE, **x})
    head = repo.commit({**SRC_HEAD, "tests/test_lib.py": "import lib\n\ndef test_a_broken():\n"
                        "    assert lib.half(3) == 99\n\ndef test_b_half():\n    assert lib.half(3) == 1.5\n\n"
                        "def test_c_double():\n    assert lib.double(3) == 6\n"})
    result = repo.gate(base, head)
    assert [(t.id.split("::")[1], t.verdict) for t in result.tests] == [
        ("test_a_broken", BROKEN_AT_HEAD), ("test_b_half", STRONG), ("test_c_double", WEAK)]


def test_stepwise_in_addopts_leaves_later_tests_collected_but_not_run(repo):
    # Fable audit 2, TSG-23. --sw stops the base run at the first failure and
    # --maxfail=0 doesn't override it. The later tests must get the reason
    # for a test that was collected but not run, not "not collected".
    sw = {"pytest.ini": "[pytest]\naddopts = --sw\n"}
    base = repo.commit({**SRC_BASE, **sw})
    head = repo.commit({**SRC_HEAD, "tests/test_lib.py": "import lib\n\ndef test_a_half():\n"
                        "    assert lib.half(3) == 1.5\n\ndef test_b_double():\n    assert lib.double(3) == 6\n"})
    result = repo.gate(base, head)
    assert [t.verdict for t in result.tests] == [STRONG, INCONCLUSIVE]
    assert result.tests[1].reason.startswith("Collected at base but not run")


def test_a_test_that_calls_pytest_exit_is_not_a_pass(repo):
    # Fable audit 2, TSG-22. pytest.exit() in a test body leaves a setup
    # record and no call record, which summarize() took for a pass, so the
    # test came out weak at base. At head, an exit leaves later tests
    # collected but not run (TSG-23).
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, "tests/test_lib.py": "import lib, pytest\n\ndef test_a_half():\n"
                        "    if lib.half(3) != 1.5:\n        pytest.exit('half is wrong')\n\n"
                        "def test_b_double():\n    assert lib.double(3) == 6\n"})
    result = repo.gate(base, head)
    assert [(t.verdict, t.reason.split(",")[0]) for t in result.tests] == [
        (INCONCLUSIVE, "Collected at base but not run"), (INCONCLUSIVE, "Collected at base but not run")]
    exits_at_head = repo.commit({"tests/test_lib.py": "import pytest\n\ndef test_a_half():\n"
                                 "    pytest.exit('stop')\n\ndef test_b_double():\n    pass\n"})
    result = repo.gate(head, exits_at_head)
    assert [t.reason.split(",")[0] for t in result.tests] == ["Collected at head but not run"] * 2


def _run_with_an_early_exit(tmp_path, *extra):
    (tmp_path / "test_x.py").write_text("import pytest\n\ndef test_a():\n    pytest.exit('stop here')\n\n"
                                        "def test_b():\n    pass\n")
    return runner.run_pytest(sys.executable, tmp_path, ["test_x.py"], tmp_path, extra,
                             select=["test_x.py::test_a", "test_x.py::test_b"])


def test_plugin_lists_tests_that_were_collected_but_not_run(tmp_path):
    # Fable audit TSG-3. items holds what was collected, and results what ran,
    # so the gate can tell "collected but not run" from "not collected".
    data = _run_with_an_early_exit(tmp_path)
    assert data["items"] == ["test_x.py::test_a", "test_x.py::test_b"]
    assert "test_x.py::test_b" not in data["results"]


@needs_xdist
def test_plugin_lists_collected_tests_under_xdist(tmp_path):
    # Under xdist the main process doesn't collect, so the workers' lists are used.
    data = _run_with_an_early_exit(tmp_path, "-n", "2")
    assert sorted(data["items"]) == ["test_x.py::test_a", "test_x.py::test_b"]


def test_a_pr_whose_source_change_is_a_non_python_file_is_not_tests_only(repo, tmp_path):
    # Fable audit TSG-4. The PR changes only a template the code reads, and
    # adds tests. It used to be "tests only", so the report said weak was
    # normal next to a strong row, and --fail-on weak never failed.
    app = "import pathlib\n\ndef render():\n    return pathlib.Path(__file__).with_name('greeting.txt').read_text().strip()\n"
    base = repo.commit({"app.py": app, "greeting.txt": "hello\n"})
    head = repo.commit({"greeting.txt": "hi there\n", "docs/notes.md": "new\n",
                        "tests/test_app.py": "import app\n\ndef test_render():\n    assert app.render() == 'hi there'\n\n"
                        "def test_render_is_text():\n    assert isinstance(app.render(), str)\n"})
    result = repo.gate(base, head)
    assert result.pr_kind == "normal"
    out = tmp_path / "r.md"
    assert cli(repo, base, head, "--fail-on", "weak", "--markdown", str(out)) == 1
    assert "Weak results are normal here" not in out.read_text()


def test_a_pr_that_changes_only_tests_and_docs_is_still_tests_only(repo):
    base = repo.commit({**SRC_BASE, "docs/notes.md": "old\n"})
    head = repo.commit({**WEAK_TEST, "docs/notes.md": "new\n"})
    assert repo.gate(base, head).pr_kind == "tests_only"


def test_a_test_file_that_is_not_utf8_is_judged(repo):
    # Fable audit TSG-6. A latin-1 test file with a coding cookie is legal
    # Python, but git show's output was decoded as strict UTF-8 and the gate
    # crashed with a traceback.
    base = repo.commit(SRC_BASE)
    (repo.path / "tests").mkdir()
    (repo.path / "tests" / "test_half.py").write_bytes(
        "# -*- coding: latin-1 -*-\nimport lib\n\ndef test_half():\n    s = 'caf\xe9'\n"
        "    assert lib.half(3) == 1.5\n".encode("latin-1"))
    head = repo.commit(SRC_HEAD)
    [test] = repo.gate(base, head).tests
    assert (test.id, test.verdict) == ("tests/test_half.py::test_half", STRONG)


def test_a_decoding_error_exits_2_with_a_message(repo, monkeypatch, capsys):
    # Fable audit TSG-6. A path that isn't UTF-8 in git's output still can't
    # be decoded, so the CLI reports it like a git error instead of crashing.
    def fail(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
    monkeypatch.setattr("test_strength_gate.cli.run_gate", fail)
    head = repo.commit(SRC_BASE)
    assert cli(repo, head, head) == 2
    assert "can't decode byte 0xe9" in capsys.readouterr().err


def test_cache_options_in_addopts_still_work(repo):
    # Fable audit TSG-10. The runner turned the cache plugin off, so --ff in
    # addopts was a usage error and every test came out inconclusive.
    ff = {"pytest.ini": "[pytest]\naddopts = --ff\n"}
    base = repo.commit({**SRC_BASE, **ff})
    head = repo.commit({**SRC_HEAD, **STRONG_TEST})
    assert [t.verdict for t in repo.gate(base, head).tests] == [STRONG]


def test_runs_write_no_cache_into_the_worktree(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert 0\n")
    data = runner.run_pytest(sys.executable, tmp_path, ["test_x.py"], tmp_path)
    assert "test_x.py::test_a" in data["results"]
    assert not (tmp_path / ".pytest_cache").exists()


def test_bytes_that_are_not_utf8_on_pytests_stderr_do_not_stop_the_gate(tmp_path):
    # Fable audit 2, TSG-27. A conftest wrote raw bytes past pytest's capture,
    # and decoding them strictly ended the gate with a message about git.
    (tmp_path / "conftest.py").write_text("import os\n\ndef pytest_sessionfinish():\n"
                                          "    os.write(2, b'\\xff\\xfe done\\n')\n")
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert 0\n")
    data = runner.run_pytest(sys.executable, tmp_path, ["test_x.py"], tmp_path)
    assert data["results"]["test_x.py::test_a"]["call"]["outcome"] == "failed"


def test_a_usage_error_gives_pytests_error_line_not_the_rootdir_line(tmp_path):
    # Fable audit TSG-10. pytest prints its usage error, then inifile: and
    # rootdir: lines, and the reason used to be the rootdir line.
    (tmp_path / "test_x.py").write_text("def test_a():\n    pass\n")
    data = runner.run_pytest(sys.executable, tmp_path, ["test_x.py"], tmp_path, ["--no-such-option"])
    assert "unrecognized arguments: --no-such-option" in data["startup_error"]


def test_annotations_are_relative_to_the_workspace_and_escaped(repo, monkeypatch, capsys):
    # Fable audit TSG-11. With the action's path input set to a subfolder,
    # the annotation's file must include it, and GitHub needs %, :, and , in
    # the file property escaped.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    base = repo.commit(SRC_BASE)
    head = repo.commit({**SRC_HEAD, "tests/test_a,b%c.py": WEAK_TEST["tests/test_lib.py"]})
    assert cli(repo, base, head, "--annotation-path-prefix", "./checkout/") == 0
    out = capsys.readouterr().out
    assert ("::warning file=checkout/tests/test_a%2Cb%25c.py,title=Weak test::"
            "tests/test_a,b%25c.py::test_double passes without the source change") in out
    assert cli(repo, base, head, "--annotation-path-prefix", ".") == 0
    assert "::warning file=tests/test_a%2Cb%25c.py,title=Weak test::" in capsys.readouterr().out
