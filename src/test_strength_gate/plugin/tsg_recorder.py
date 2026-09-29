"""pytest plugin that records what happened to each test, as JSON.

test-strength-gate loads this with `-p tsg_recorder` after putting this
directory on PYTHONPATH. It runs inside the environment under test, so it
imports only the standard library and pytest.

Settings come from environment variables:
  TSG_OUT            where to write the JSON (required)
  TSG_ROOT           the worktree root; IDs are made relative to it
  TSG_SELECT         optional JSON list of IDs; every other test is deselected
  TSG_ORIGINAL_REPO  optional path of the real checkout, to spot imports from it
  TSG_WATCH          optional JSON list of worktree-relative .py paths, to report which were imported or read

The JSON has five keys:
  items           IDs of collected (and selected) tests; one listed here but
                  missing from results was collected but not run
  collect_errors  {file: last error line} for files that failed to import
  results         {id: {phase: {outcome, exc_type, message, attr_owner, missing_path,
                                raised_at, raised_inside, local_at, source_line,
                                stderr_hint, xfail}}}
  misrouted       {module: path} for project modules imported from outside the worktree,
                  found in this process and, under pytest-xdist, in each worker
  copied_imported the TSG_WATCH paths that some imported module was loaded from
"""

import json
import linecache
import os
import re
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(os.environ.get("TSG_ROOT", ".")).resolve()
_data = {"items": [], "collect_errors": {}, "results": {}, "misrouted": {}, "copied_imported": []}
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


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    """Under pytest-xdist the main process collects nothing, so take each worker's list."""
    for nodeid in ids:
        test_id = _canonical_id(_config.rootpath / nodeid.split("::")[0], nodeid)
        if test_id not in _data["items"]:
            _data["items"].append(test_id)


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


def _inside(path):
    try:
        return Path(path).resolve().is_relative_to(_ROOT)
    except (OSError, ValueError):
        return False


def _where(path, line):
    """'path:line', with the path relative to the worktree when it is inside it."""
    try:
        path = Path(path).resolve().relative_to(_ROOT).as_posix()
    except (OSError, ValueError):
        pass
    return f"{path}:{line}"


def _locations(exc):
    """Where the exception was raised, from the traceback.

    raised_at      the innermost frame, which may be in a library
    raised_inside  whether that frame is a file in the worktree
    local_at       the innermost frame that is in the worktree, i.e. the last
                   line of the test's or the project's own code that ran
    """
    tb, innermost, local = exc.tb, None, None
    while tb is not None:
        code = tb.tb_frame.f_code
        innermost = (code.co_filename, tb.tb_lineno)
        if _inside(code.co_filename):
            local = innermost
        tb = tb.tb_next
    if innermost is None:
        return {"raised_at": None, "raised_inside": False, "local_at": None, "local_frame": None}
    return {"raised_at": _where(*innermost), "raised_inside": _inside(innermost[0]),
            "local_at": _where(*local) if local else None, "local_frame": local}


_ERROR_LINE = re.compile(r"error|exception|traceback", re.IGNORECASE)


def _bare_assertion_hints(exc, local_frame, report):
    """For an AssertionError with no message, the failing line and a stderr hint.

    pytest rewrites asserts only in test modules and conftest.py, so a bare
    assert in a helper fails with an empty message, and the report would only
    say "AssertionError". The source line of the innermost frame in the
    worktree shows what was checked. The last captured stderr line that names
    an error, or else the last line, often shows why (e.g. an unknown option
    rejected by a CLI). Both are cut to 200 characters, and nothing else from
    the captured output is kept.
    """
    if not isinstance(exc.value, AssertionError) or str(exc.value):
        return {"source_line": None, "stderr_hint": None}
    source = linecache.getline(*local_frame).strip()[:200] if local_frame else ""
    lines = [ln.strip() for ln in (getattr(report, "capstderr", "") or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if _ERROR_LINE.search(ln)]
    hint = (errors or lines or [""])[-1][:200]
    return {"source_line": source or None, "stderr_hint": hint or None}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # The exception type is only available here, on the worker that ran the
    # test. Attach it to the report so it also reaches the main process under
    # pytest-xdist, where pytest_runtest_logreport records it.
    outcome = yield
    report = outcome.get_result()
    exc = call.excinfo
    where = _locations(exc) if exc else {}
    local_frame = where.pop("local_frame", None)
    report.user_properties.append(("tsg", {
        "id": _canonical_id(item.path, item.nodeid),
        "exc_type": exc.type.__name__ if exc else None,
        "message": str(exc.value)[:500] if exc else None,
        "attr_owner": _attr_owner(exc.value) if exc else None,
        "missing_path": _missing_path(exc.value) if exc else None,
        **where,
        **(_bare_assertion_hints(exc, local_frame, report) if exc else {}),
        "xfail": hasattr(report, "wasxfail"),
        # Under pytest-xdist the worker, not the main process, imports the
        # project, so only the worker can see where the modules came from.
        **({"misrouted": _worker_misrouted(), "copied_imported": _watched_imports()}
           if hasattr(item.config, "workerinput") else {}),
    }))


def pytest_runtest_logreport(report):
    info = dict(report.user_properties).get("tsg")
    if info is None:
        return
    for module, path in (info.get("misrouted") or {}).items():
        _data["misrouted"].setdefault(module, path)
    for path in info.get("copied_imported") or ():
        if path not in _data["copied_imported"]:
            _data["copied_imported"].append(path)
    phases = _data["results"].setdefault(info["id"], {})
    phases[report.when] = {
        "outcome": report.outcome,
        "exc_type": info["exc_type"],
        "message": info["message"],
        "attr_owner": info["attr_owner"],
        "missing_path": info["missing_path"],
        "raised_at": info.get("raised_at"),
        "raised_inside": info.get("raised_inside", False),
        "local_at": info.get("local_at"),
        "source_line": info.get("source_line"),
        "stderr_hint": info.get("stderr_hint"),
        "xfail": info["xfail"],
    }


# Folders whose packages are not the project's importable code: tests, docs,
# examples, vendored copies of other projects, and build or environment output.
_NOT_PROJECT_CODE = {"tests", "test", "testing", "docs", "doc", "examples", "example", "benchmarks",
                     "scripts", "vendor", "vendored", "_vendor", "third_party", "build", "dist",
                     "node_modules", "site-packages", "venv", "env"}


def _project_top_level_names():
    """Top-level module and package names that the worktree defines.

    That is every .py file and package at the worktree root or in src/, and
    every top-level package anywhere else: a folder with __init__.py whose
    parent has none, such as lib/mypkg. Hidden folders and the folders in
    _NOT_PROJECT_CODE are not searched. Namespace packages (no __init__.py)
    outside the root and src/ are not found.
    """
    names = set()
    for folder in (_ROOT, _ROOT / "src"):
        if folder.is_dir():
            names |= {p.stem for p in folder.glob("*.py")}
            names |= {p.parent.name for p in folder.glob("*/__init__.py")}
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        here = Path(dirpath)
        if here != _ROOT and "__init__.py" in filenames and not (here.parent / "__init__.py").exists():
            names.add(here.name)
            dirnames[:] = []  # its subpackages share its top-level name
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _NOT_PROJECT_CODE]
    return names


_project_names = None


def _misrouted_imports(names=None):
    """Project modules that were imported from outside the worktree.

    If the project is installed (pip install . or pip install -e .), its
    modules can load from site-packages or from the real checkout instead of
    the worktree, and then the base run tests head code. A project module is
    one whose top-level name the worktree defines (see
    _project_top_level_names), or any module loaded from the real checkout
    outside its virtualenv.
    """
    original = os.environ.get("TSG_ORIGINAL_REPO")
    original = Path(original).resolve() if original else None
    prefixes = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    global _project_names
    if _project_names is None:
        _project_names = _project_top_level_names()
    local = _project_names
    found = {}
    for name in (sys.modules if names is None else names):
        module = sys.modules.get(name)
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


_scanned = set()
_worker_found = {}


def _worker_misrouted():
    """_misrouted_imports for an xdist worker, checking only modules not seen before.

    It runs after every test phase, so it looks only at the modules imported
    since the last call and returns everything found so far in this worker.
    """
    new = [name for name in list(sys.modules) if name not in _scanned]
    _scanned.update(new)
    for module, path in _misrouted_imports(new).items():
        _worker_found.setdefault(module, path)
    return dict(_worker_found)


_watch = None
_watch_dirs = {}
_watch_scanned = set()
_watch_found = set()


def _watched_imports():
    """The TSG_WATCH paths that the run imported, or read from a module beside them.

    The gate copies git-ignored .py files, such as a version.py written at
    install time, from the checkout into the base worktree. They were
    generated for head, so the gate needs to know whether the base run used
    them. A package can read such a file without importing it, e.g. with
    exec(open(...).read()) in its __init__.py, so a copied file also counts
    when an imported module in the same folder names it in its source. It
    runs after every test phase under xdist, so it looks only at modules
    imported since the last call.
    """
    global _watch
    if _watch is None:
        _watch = {}
        for rel in json.loads(os.environ.get("TSG_WATCH") or "[]"):
            path = (_ROOT / rel).resolve()
            _watch[path] = rel
            _watch_dirs.setdefault(path.parent, []).append((path.stem, rel))
    if not _watch:
        return []
    new = [name for name in list(sys.modules) if name not in _watch_scanned]
    _watch_scanned.update(new)
    for name in new:
        file = getattr(sys.modules.get(name), "__file__", None)
        if not file:
            continue
        path = Path(file).resolve()
        if path in _watch:
            _watch_found.add(_watch[path])
        elif path.parent in _watch_dirs and path.suffix == ".py":
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for stem, rel in _watch_dirs[path.parent]:
                if re.search(rf"\b{re.escape(stem)}\b", text):
                    _watch_found.add(rel)
    return sorted(_watch_found)


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):  # an xdist worker; the main process writes
        return
    # Without xdist this process ran the tests. With xdist the workers' finds
    # arrived with their reports and are already in _data["misrouted"].
    for module, path in _misrouted_imports().items():
        _data["misrouted"].setdefault(module, path)
    _data["copied_imported"] = sorted(set(_data["copied_imported"]) | set(_watched_imports()))
    Path(os.environ["TSG_OUT"]).write_text(json.dumps(_data, indent=2))
