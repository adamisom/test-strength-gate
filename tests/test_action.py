"""Run the action's shell step, taken from action.yml, the way a runner would.

This is not a GitHub workflow run. It checks the parts of the composite
action that can run locally: the default base and head from a shallow merge
commit, inputs passed through env variables, the outputs file, the job
summary, the annotations and the exit code.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ACTION = Path(__file__).resolve().parent.parent / "action.yml"


def step_script(name):
    """The `run: |` block of the step called `name`, dedented. action.yml is simple enough to read by hand."""
    lines = ACTION.read_text().splitlines()
    start = lines.index(f"    - name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(" " * 8):
            break
        body.append(line[8:])
    return "\n".join(body)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_action_run_step_on_a_shallow_merge_commit(repo, tmp_path):
    repo.commit({"lib.py": "def double(x):\n    return x + x\n\ndef half(x):\n    return x // 2\n"})
    repo.git("checkout", "-q", "-b", "feature")
    pr_head = repo.commit({"lib.py": "def double(x):\n    return 2 * x\n\ndef half(x):\n    return x / 2\n",
                           "tests/test_lib.py": "import lib\n\ndef test_double():\n    assert lib.double(3) == 6\n\n"
                                                "def test_half():\n    assert lib.half(3) == 1.5\n"})
    repo.git("checkout", "-q", "main")
    repo.commit({"README": "main moved on\n"})
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "merge")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "2", f"file://{repo.path}", str(clone)], check=True)

    temp, out, summary = tmp_path / "runner-temp", tmp_path / "github-output", tmp_path / "summary.md"
    temp.mkdir()
    env = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(tmp_path), "GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(temp),
           "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
           # The action's inputs, as the step's env block passes them.
           "BASE_SHA": "", "HEAD_SHA": "", "PR_HEAD_SHA": pr_head, "TEST_GLOBS": "tests/*.py **/conftest.py",
           "PYTEST_ARGS": "-o addopts=", "FAIL_ON": "weak", "PYTHON_BIN": sys.executable}
    proc = subprocess.run(["bash", "-c", step_script("Run test-strength-gate")], cwd=clone, env=env,
                          capture_output=True, text=True)

    assert proc.returncode == 1, proc.stderr  # fail-on: weak, and test_double is weak
    assert "::warning file=tests/test_lib.py,title=Weak test::tests/test_lib.py::test_double" in proc.stdout
    assert "::warning::The merge commit" not in proc.stdout  # HEAD^2 is the PR head
    assert out.read_text() == f"json-report={temp}/test-strength-gate.json\n"
    report = json.loads((temp / "test-strength-gate.json").read_text())
    assert {t["id"]: t["verdict"] for t in report["tests"]} == {
        "tests/test_lib.py::test_double": "WEAK", "tests/test_lib.py::test_half": "STRONG"}
    assert "2 new tests: 1 strong, 1 weak" in summary.read_text()


def run_install_step(tmp_path, python, path):
    env = {"PATH": path, "HOME": str(tmp_path), "PYTHON_BIN": str(python), "ACTION_PATH": str(ACTION.parent),
           "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))}
    return subprocess.run(["bash", "-c", step_script("Install test-strength-gate")], env=env,
                          capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_step_without_pip_or_uv_fails_with_a_clear_error(tmp_path):
    # Fable audit TSG-8. A Python made without pip (as uv venv makes it) used
    # to fail with "No module named pip". With no uv either, say what to do.
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tmp_path / "venv")], check=True)
    proc = run_install_step(tmp_path, tmp_path / "venv" / "bin" / "python", "/usr/bin:/bin")
    assert proc.returncode == 1
    assert "::error::" in proc.stdout and "install pip or uv" in proc.stdout


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("uv") is None, reason="needs bash and uv")
def test_install_step_uses_uv_when_the_python_has_no_pip(tmp_path):
    # Fable audit TSG-8. A uv-made virtualenv has no pip, so install with uv.
    uv = shutil.which("uv")
    subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(tmp_path / "venv")], check=True)
    python = tmp_path / "venv" / "bin" / "python"
    proc = run_install_step(tmp_path, python, f"{Path(uv).parent}:/usr/bin:/bin")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    check = subprocess.run([str(python), "-c", "import test_strength_gate.cli"], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
