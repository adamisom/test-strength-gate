"""Turn raw pytest results at base and head into one verdict per test."""

import re
from dataclasses import dataclass

STRONG = "STRONG"
WEAK = "WEAK"
INCONCLUSIVE = "INCONCLUSIVE"
SKIPPED = "SKIPPED"
BROKEN_AT_HEAD = "BROKEN_AT_HEAD"
VERDICTS = [STRONG, WEAK, INCONCLUSIVE, SKIPPED, BROKEN_AT_HEAD]


# --- The exception-type rules. Change them here. -----------------------------

# Errors that usually mean the test calls something the base code doesn't
# have yet (a new module, function or name). The test fails at base, but not
# because it checked behavior, so the failure proves little.
MISSING_API_ERRORS = {"ImportError", "ModuleNotFoundError", "NameError"}

# A TypeError whose message is about the call signature means the same thing,
# e.g. a new keyword argument. Other TypeErrors are treated as behavior.
# Python raises it in the frame that made the call, so when that frame is in
# the project's own code rather than in the test, the old code may simply have
# called something wrongly. The verdict stays inconclusive, but the reason
# says the cause is unclear. (A decorator's *args/**kwargs wrapper in the
# project raises there too, for a real new keyword, so this can't be strong.)
ARGUMENT_MISMATCH = re.compile(
    r"unexpected keyword argument|positional argument|required (?:keyword|positional)"
    r"|takes no arguments|takes \d+|missing \d+ required"
)


def failure_kind(exc_type, message, attr_owner=None, file_at_head=False, origin=""):
    """Say what a call-phase failure at base tells us about the test.

    Returns one of:
      "assertion"    the test checked a result and it was wrong (STRONG)
      "missing_api"  the test used an API base doesn't have (INCONCLUSIVE)
      "missing_file" the test read a file that exists at head but not in the
                     base run, e.g. a data file the PR adds (INCONCLUSIVE)
      "unclear_call" a TypeError about arguments raised inside the project's
                     code: a new API or a bug in the old code (INCONCLUSIVE)
      "other_error"  base raised some other exception; the test still saw
                     base behave differently from head (STRONG)

    attr_owner is what kind of object lacked the attribute, for an
    AttributeError: module, class, object, builtin, or None if unknown.
    file_at_head is True for a FileNotFoundError whose path exists in the
    head commit. origin is where the exception was raised: test (a file that
    matches the test patterns), project (any other file in the worktree),
    library (outside the worktree), or "" if unknown.
    """
    # pytest.raises(...) failing with "DID NOT RAISE", and pytest.fail(),
    # both raise pytest's Failed exception. Both are checks that failed.
    if exc_type in ("AssertionError", "Failed"):
        return "assertion"
    if exc_type in MISSING_API_ERRORS:
        return "missing_api"
    if exc_type == "FileNotFoundError" and file_at_head:
        # The input differs between the runs, not the code: the file wasn't
        # copied to base, or the PR adds it for the code to use.
        return "missing_file"
    if exc_type == "AttributeError":
        # calc.new_func or obj.new_method is a missing API. But None.value or
        # "text".items means base returned the wrong kind of value, which is
        # behavior. Unknown owners (monkeypatch, mock.patch) count as missing.
        return "other_error" if attr_owner == "builtin" else "missing_api"
    if exc_type == "TypeError" and ARGUMENT_MISMATCH.search(message or ""):
        return "unclear_call" if origin == "project" else "missing_api"
    return "other_error"


# --- Summarizing one test's phases ------------------------------------------

@dataclass
class Outcome:
    status: str          # passed, failed, error, skipped, not_run
    phase: str = ""      # setup, call, teardown, collect, startup, misrouted
    exc_type: str = ""
    message: str = ""
    attr_owner: str = ""
    missing_path: str = ""   # for FileNotFoundError: the path, relative to the worktree
    file_at_head: bool = False  # set by the gate: missing_path exists in the head commit
    raised_at: str = ""      # "path:line" of the frame that raised
    raised_inside: bool = False  # that frame is a file in the worktree, not a library
    local_at: str = ""       # "path:line" of the last frame in the worktree
    origin: str = ""         # set by the gate from raised_at: test, project, library

    def location(self):
        """Where the exception came from, for the reason text."""
        if self.raised_inside:
            return f"raised at {self.raised_at}"
        if self.local_at:
            return f"raised in library code called from {self.local_at}"
        return ""

    def describe(self):
        if self.status in ("passed", "skipped"):
            return self.status
        if self.status == "not_run":
            return "not run"
        where = "failed" if self.status == "failed" else f"error in {self.phase}"
        # pytest.raises and pytest.fail raise "Failed"; its message says more.
        label = _short(self.message, 30) if self.exc_type == "Failed" else self.exc_type
        return f"{where} ({label})" if label else where


def summarize(phases, collect_error=None, startup_error=None):
    """Collapse a test's per-phase records (from the plugin) into one Outcome."""
    if not phases:
        if collect_error:
            return Outcome("error", "collect", message=collect_error)
        if startup_error:
            return Outcome("error", "startup", message=startup_error)
        return Outcome("not_run", message="not collected")
    for phase in ("setup", "call", "teardown"):
        rec = phases.get(phase)
        if rec is None:
            continue
        if rec["outcome"] == "skipped":
            return Outcome("skipped", phase, message="xfail" if rec.get("xfail") else "")
        if rec["outcome"] == "failed":
            status = "failed" if phase == "call" else "error"
            return Outcome(status, phase, rec.get("exc_type") or "", rec.get("message") or "",
                           rec.get("attr_owner") or "", rec.get("missing_path") or "",
                           raised_at=rec.get("raised_at") or "", raised_inside=bool(rec.get("raised_inside")),
                           local_at=rec.get("local_at") or "")
    return Outcome("passed", "call")


def _short(text, limit=90):
    """First line of a message, trimmed. pytest's assertion messages add '+ where' lines."""
    lines = (text or "").strip().splitlines()
    text = " ".join(lines[0].split()) if lines else ""
    text = re.sub(r" \((?:/|[A-Za-z]:\\)[^)]*\)$", "", text)  # ImportError's "(/path/to/mod.py)"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def verdict(base, head):
    """Return (verdict, one-line reason) from base and head Outcomes."""
    if head.status == "skipped":
        return SKIPPED, "Skipped at head, so it can't be judged."
    if head.status != "passed":
        detail = f": {_short(head.message)}" if head.message else ""
        return BROKEN_AT_HEAD, f"Does not pass at head ({head.describe()}{detail})."

    if base.status == "passed":
        return WEAK, "Passes without the source change, so check that it exercises the change."
    if base.status == "skipped":
        return SKIPPED, "Skipped at base."
    if base.status == "not_run":
        return INCONCLUSIVE, "Not collected at base (its ID may depend on base code)."
    if base.status == "error":
        what = {"collect": "The file fails to import at base",
                "startup": "pytest could not start at base",
                "setup": "Setup or fixture error at base",
                "teardown": "Teardown error at base",
                "misrouted": "The base run loaded project code from outside the base worktree"}[base.phase]
        return INCONCLUSIVE, f"{what}: {_short(base.message)}"

    kind = failure_kind(base.exc_type, base.message, base.attr_owner or None, base.file_at_head, base.origin)
    if kind == "missing_file":
        return INCONCLUSIVE, (f"Fails at base because it reads {base.missing_path}, which exists at head but "
                              f"not in the base run. If it is test data, add a --test-glob that matches it.")
    if kind == "unclear_call":
        return INCONCLUSIVE, (f"Fails at base with a TypeError about arguments raised inside the base code at "
                              f"{base.raised_at}. The test may call a new API, or the old code may call something "
                              f"wrongly, which would be a real failure: {_short(base.message)}")
    if kind == "assertion":
        return STRONG, f"Fails at base on a check: {_short(base.message) or base.exc_type}"
    if kind == "missing_api":
        return INCONCLUSIVE, (f"Fails at base with {base.exc_type}, which usually means the new API "
                              f"doesn't exist yet: {_short(base.message)}")
    # Not an assertion, so show where it was raised: in the old code, in the
    # test's own code, or in a library. A reviewer can then check the cause.
    where = f", {base.location()}" if base.location() else ""
    return STRONG, f"Fails at base with {base.exc_type} (not an assertion{where}): {_short(base.message)}"
