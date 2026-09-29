"""Run pytest in a worktree with the recorder plugin and read back its JSON."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent / "plugin"


def run_pytest(python, worktree, files, original_repo, extra_args=(), select=None, collect_only=False,
               timeout=900, watch=()):
    """Run pytest on `files` inside `worktree` and return the recorder's data.

    `select` is a list of test IDs to run; the plugin deselects the rest.
    `watch` is a list of worktree-relative .py paths; the result's
    'copied_imported' says which of them the run imported.
    Passing node IDs on the command line is not safe, because one ID that
    does not exist at base makes pytest abort the whole run.

    If pytest produces no results because it could not start (e.g. a
    conftest.py on the command line fails to import, which exits with code 4
    before any test is collected) or it timed out, the result has a
    'startup_error' with the reason, and every test in the run is inconclusive.
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
        env.update(TSG_OUT=str(out), TSG_ROOT=str(worktree), TSG_ORIGINAL_REPO=str(original_repo),
                   # Stale .pyc files could otherwise outlive a source swap.
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("TSG_SELECT", None)
        env.pop("TSG_WATCH", None)
        if watch:
            env["TSG_WATCH"] = json.dumps(sorted(watch))
        if select is not None:
            select_file = Path(tmp) / "select.json"
            select_file.write_text(json.dumps(sorted(select)))
            env["TSG_SELECT"] = str(select_file)

        # --maxfail=0 comes after the project's own arguments, so a -x or
        # --maxfail in addopts or pytest-args can't stop the run at the first
        # failure and leave the later judged tests without a result. The cache
        # goes to this run's temp dir rather than into the worktree; the cache
        # plugin stays on, because options such as --ff in addopts belong to it.
        cmd = [python, "-m", "pytest", "-p", "tsg_recorder", "--continue-on-collection-errors", "-q",
               *extra_args, "--maxfail=0", "-o", f"cache_dir={Path(tmp) / 'cache'}"]
        if collect_only:
            cmd.append("--collect-only")
        cmd += list(files)
        empty = {"items": [], "collect_errors": {}, "results": {}, "misrouted": {}, "copied_imported": []}
        try:
            proc = subprocess.run(cmd, cwd=worktree, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return {**empty, "startup_error": f"pytest timed out after {timeout} seconds"}

        data = json.loads(out.read_text()) if out.exists() else dict(empty)
        # Exit codes 2 (interrupted), 3 (internal error) and 4 (usage error)
        # with nothing recorded mean the run as a whole failed.
        recorded = data["results"] or data["items"] or data["collect_errors"]
        if not recorded and (not out.exists() or proc.returncode in (2, 3, 4)):
            lines = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]
            errors = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
            # A usage error has no "E " line, and pytest prints inifile: and
            # rootdir: lines after it, so look for the line that says "error:".
            errors = errors or [ln for ln in lines if "error:" in ln.lower()]
            data["startup_error"] = (errors or lines or [f"pytest exited with code {proc.returncode}"])[-1]
        return data
