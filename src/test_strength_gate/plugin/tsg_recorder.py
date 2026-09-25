"""pytest plugin that records what happened to each test, as JSON.

test-strength-gate loads this with `-p tsg_recorder` after putting this
directory on PYTHONPATH. It runs inside the environment under test, so it
imports only the standard library and pytest.

Settings come from environment variables:
  TSG_OUT            where to write the JSON (required)
  TSG_ROOT           the worktree root; IDs are made relative to it
  TSG_SELECT         optional JSON list of IDs; every other test is deselected
  TSG_ORIGINAL_REPO  optional path of the real checkout, to spot imports from it

The JSON has four keys:
  items           IDs of collected (and selected) tests
  collect_errors  {file: last error line} for files that failed to import
  results         {id: {phase: {outcome, exc_type, message, attr_owner, missing_path, xfail}}}
  misrouted       {module: path} for project modules imported from outside the worktree
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(os.environ.get("TSG_ROOT", ".")).resolve()
_data = {"items": [], "collect_errors": {}, "results": {}, "misrouted": {}}
_config = None


def _canonical_id(path, nodeid):
    """Turn a pytest node ID into '<path relative to worktree>::<rest>'.

    pytest node IDs are relative to its rootdir, which depends on ini files
    and arguments. Paths relative to the worktree root are stable, and they
    are also valid pytest arguments when pytest runs from the worktree root.
    """
    try:
        rel = Path(path).resolve().relative_to(_ROOT).as_posix()
    except ValueError:
        rel = str(path)
    rest = nodeid.split("::", 1)[1] if "::" in nodeid else ""
    return f"{rel}::{rest}" if rest else rel


def pytest_configure(config):
    global _config
    _config = config


def pytest_collection_modifyitems(config, items):
    select_file = os.environ.get("TSG_SELECT")
    if not select_file:
        return
    wanted = set(json.loads(Path(select_file).read_text()))
    keep, drop = [], []
    for item in items:
        (keep if _canonical_id(item.path, item.nodeid) in wanted else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_collection_finish(session):
    _data["items"] = [_canonical_id(i.path, i.nodeid) for i in session.items]


def pytest_collectreport(report):
    if not report.failed:
        return
    path = _config.rootpath / report.nodeid.split("::")[0]
    lines = [ln for ln in report.longreprtext.splitlines() if ln.strip()]
    errors = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
    message = errors[-1] if errors else (lines[-1] if lines else "collection error")
    _data["collect_errors"][_canonical_id(path, "")] = message


def _attr_owner(exc):
    """For an AttributeError, what kind of object lacked the attribute.

    Python 3.10+ sets .name and .obj when attribute lookup fails. They are
    unset (name is None) when code raises AttributeError itself, as
    monkeypatch and mock.patch do.
    """
    if not isinstance(exc, AttributeError) or getattr(exc, "name", None) is None:
        return None
    obj = exc.obj
    if isinstance(obj, types.ModuleType):
        return "module"
    if isinstance(obj, type):
        return "class"
    if type(obj).__module__ == "builtins":  # None, str, int, dict, ...
        return "builtin"
    return "object"


def _missing_path(exc):
    """For a FileNotFoundError on a path inside the worktree, that path, relative to it.

    The gate uses it to tell a file the PR adds (and the base run lacks) from
    a file the code under test failed to create.
    """
    if not isinstance(exc, FileNotFoundError) or not isinstance(exc.filename, (str, bytes, os.PathLike)):
        return None
    try:
        path = Path(os.path.abspath(os.fsdecode(exc.filename))).resolve()
        return path.relative_to(_ROOT).as_posix()
    except (ValueError, OSError):
        return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # The exception type is only available here, on the worker that ran the
    # test. Attach it to the report so it also reaches the main process under
    # pytest-xdist, where pytest_runtest_logreport records it.
    outcome = yield
    report = outcome.get_result()
    exc = call.excinfo
    report.user_properties.append(("tsg", {
        "id": _canonical_id(item.path, item.nodeid),
        "exc_type": exc.type.__name__ if exc else None,
        "message": str(exc.value)[:500] if exc else None,
        "attr_owner": _attr_owner(exc.value) if exc else None,
        "missing_path": _missing_path(exc.value) if exc else None,
        "xfail": hasattr(report, "wasxfail"),
    }))


def pytest_runtest_logreport(report):
    info = dict(report.user_properties).get("tsg")
    if info is None:
        return
    phases = _data["results"].setdefault(info["id"], {})
    phases[report.when] = {
        "outcome": report.outcome,
        "exc_type": info["exc_type"],
        "message": info["message"],
        "attr_owner": info["attr_owner"],
        "missing_path": info["missing_path"],
        "xfail": info["xfail"],
    }


def _misrouted_imports():
    """Project modules that were imported from outside the worktree.

    If the project is installed (pip install . or pip install -e .), its
    modules can load from site-packages or from the real checkout instead of
    the worktree, and then the base run tests head code. A project module is
    one whose top-level name exists at the worktree root or in src/, or any
    module loaded from the real checkout outside its virtualenv.
    """
    original = os.environ.get("TSG_ORIGINAL_REPO")
    original = Path(original).resolve() if original else None
    prefixes = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    local = set()
    for folder in (_ROOT, _ROOT / "src"):
        if folder.is_dir():
            local |= {p.stem for p in folder.glob("*.py")}
            local |= {p.parent.name for p in folder.glob("*/__init__.py")}
    found = {}
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = Path(file).resolve()
        if path.is_relative_to(_ROOT):
            continue
        top = name.split(".")[0]
        from_checkout = (original is not None and path.is_relative_to(original)
                         and not any(path.is_relative_to(p) for p in prefixes))
        if top in local or from_checkout:
            found.setdefault(top, str(path))
    return found


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):  # an xdist worker; the main process writes
        return
    _data["misrouted"] = _misrouted_imports()
    Path(os.environ["TSG_OUT"]).write_text(json.dumps(_data, indent=2))
