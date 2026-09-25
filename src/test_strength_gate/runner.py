"""Run pytest in a worktree with the recorder plugin and read back its JSON."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent / "plugin"


def run_pytest(python, worktree, files, original_repo, extra_args=(), select=None, collect_only=False):
    """Run pytest on `files` inside `worktree` and return the recorder's data.

    `select` is a list of test IDs to run; the plugin deselects the rest.
    Passing node IDs on the command line is not safe, because one ID that
    does not exist at base makes pytest abort the whole run.

    If pytest dies before writing its JSON (e.g. a conftest.py fails to
    import), the result has a 'startup_error' with the end of its output.
    """
    worktree = Path(worktree)
    with tempfile.TemporaryDirectory(prefix="tsg-run-") as tmp:
        out = Path(tmp) / "out.json"
        env = os.environ.copy()
        # The worktree comes before site-packages on sys.path, so its code
        # wins over an installed or editable copy of the project.
        paths = [str(PLUGIN_DIR), str(worktree)]
        if (worktree / "src").is_dir():
            paths.append(str(worktree / "src"))
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env.update(TSG_OUT=str(out), TSG_ROOT=str(worktree), TSG_ORIGINAL_REPO=str(original_repo))
        env.pop("TSG_SELECT", None)
        if select is not None:
            select_file = Path(tmp) / "select.json"
            select_file.write_text(json.dumps(sorted(select)))
            env["TSG_SELECT"] = str(select_file)

        cmd = [python, "-m", "pytest", "-p", "tsg_recorder", "-p", "no:cacheprovider",
               "--continue-on-collection-errors", "-q", *extra_args]
        if collect_only:
            cmd.append("--collect-only")
        cmd += list(files)
        proc = subprocess.run(cmd, cwd=worktree, env=env, capture_output=True, text=True)

        if not out.exists():
            lines = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]
            errors = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
            message = (errors or lines or [f"pytest exited with code {proc.returncode}"])[-1]
            return {"items": [], "collect_errors": {}, "results": {}, "leaked_imports": [],
                    "startup_error": message}
        return json.loads(out.read_text())
