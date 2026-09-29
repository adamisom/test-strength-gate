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
# Python writes these messages itself, and each starts with the callable's
# name, e.g. "add() got an unexpected keyword argument 'c'", "f() takes 2
# positional arguments but 3 were given", "len() takes exactly one argument
# (2 given)", or, for some C functions, "divmod expected 2 arguments, got 1".
# The pattern is anchored to that shape, so a project's own TypeError whose
# words only sound like one, e.g. "renderer takes at most 2 arguments for this
# input", stays behavior (Codex TSG-16). A few other shapes are just as
# fixed: Django's and SQLAlchemy's for a model field that doesn't exist yet,
# "Author() got unexpected keyword arguments: 'nickname'" and "'nickname' is
# an invalid keyword argument for Author", and CPython's Argument Clinic
# wordings, "'strict' is an invalid keyword argument for int()", "open()
# missing required argument 'file' (pos 1)" and "this function got an
# unexpected keyword argument 'x'" (Fable audit 2, TSG-17 and TSG-21).
_CALLABLE = r"[\w.<>]+"
ARGUMENT_MISMATCH = re.compile(
    rf"^{_CALLABLE}\(\) (?:got an unexpected keyword argument|got multiple values for argument"
    rf"|got some positional-only argument|takes |missing \d+ required|missing required argument "
    rf"|got unexpected keyword arguments?:)"
    rf"|^{_CALLABLE} expected (?:(?:at most|at least|exactly) )?\d+ arguments?, got \d+"
    rf"|^'\w+' is an invalid keyword argument for {_CALLABLE}"
    rf"|^this function got an unexpected keyword argument"
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
    phase: str = ""      # setup, call, teardown, collect, startup, misrouted; for not_run,
                         # "collected" if pytest collected the test but never ran it
    exc_type: str = ""
    message: str = ""
    attr_owner: str = ""
    missing_path: str = ""   # for FileNotFoundError: the path, relative to the worktree
    file_at_head: bool = False  # set by the gate: missing_path exists in the head commit
    raised_at: str = ""      # "path:line" of the frame that raised
    raised_inside: bool = False  # that frame is a file in the worktree, not a library
    local_at: str = ""       # "path:line" of the last frame in the worktree
    origin: str = ""         # set by the gate from raised_at: test, project, library
    source_line: str = ""    # for an AssertionError with no message: the failing line
    stderr_hint: str = ""    # ... and the last captured stderr line that names an error

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
            return "not collected" if self.message == "not collected" else "not run"
        where = "failed" if self.status == "failed" else f"error in {self.phase}"
        # pytest.raises and pytest.fail raise "Failed"; its message says more.
        label = _short(self.message, 30) if self.exc_type == "Failed" else self.exc_type
        return f"{where} ({label})" if label else where


def summarize(phases, collect_error=None, startup_error=None, collected=False):
    """Collapse a test's per-phase records (from the plugin) into one Outcome.

    collected is True when the plugin listed the test among the collected
    items, so a test without records was collected but never ran, e.g.
    because something stopped the session early.
    """
    if not phases:
        if collect_error:
            return Outcome("error", "collect", message=collect_error)
        if startup_error:
            return Outcome("error", "startup", message=startup_error)
        if collected:
            return Outcome("not_run", "collected", message="collected but not run")
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
                           local_at=rec.get("local_at") or "", source_line=rec.get("source_line") or "",
                           stderr_hint=rec.get("stderr_hint") or "")
    if "call" not in phases:
        # Setup passed but the body never reported, e.g. it called
        # pytest.exit(), which ends the session without a call record.
        return Outcome("not_run", "collected", message="collected but not run")
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
    if head.status == "error" and head.phase == "startup":
        # A timeout or internal error ended the whole run, so the test's own
        # result at head is unknown. That is not the same as failing there.
        return INCONCLUSIVE, f"pytest failed as a whole at head, so the test was not judged: {_short(head.message)}"
    if head.status == "not_run":
        # No result at head is not a failure there (see the startup case above).
        if head.phase == "collected":
            return INCONCLUSIVE, ("Collected at head but not run, e.g. because the session stopped early, "
                                  "so its result at head is unknown.")
        return INCONCLUSIVE, ("Not collected at head (its ID may change between runs), "
                              "so its result at head is unknown.")
    if head.status != "passed":
        detail = f": {_short(head.message)}" if head.message else ""
        return BROKEN_AT_HEAD, f"Does not pass at head ({head.describe()}{detail})."

    if base.status == "passed":
        return WEAK, "Passes without the source change, so check that it exercises the change."
    if base.status == "skipped":
        return SKIPPED, "Skipped at base."
    if base.status == "not_run":
        if base.phase == "collected":
            return INCONCLUSIVE, ("Collected at base but not run, e.g. because the session stopped early, "
                                  "so its result at base is unknown.")
        return INCONCLUSIVE, "Not collected at base (its ID may depend on base code)."
    if base.status == "error":
        what = {"collect": "The file fails to import at base",
                "startup": "pytest failed as a whole at base",
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
        if base.message.strip():
            return STRONG, f"Fails at base on a check: {_short(base.message)}"
        # A bare assert (often in a helper pytest doesn't rewrite) says nothing
        # by itself, and the real cause can be a missing API further up, e.g.
        # a CLI rejecting a new option. Show the line and any stderr error.
        reason = "Fails at base on a check that has no message"
        if base.local_at:
            reason += f", at {base.local_at}"
        if base.source_line:
            reason += f": `{_short(base.source_line, 80)}`"
        if base.stderr_hint:
            reason += f". Last error on stderr: {_short(base.stderr_hint, 120)}"
        return STRONG, reason + "."
    if kind == "missing_api":
        return INCONCLUSIVE, (f"Fails at base with {base.exc_type}, which usually means the new API "
                              f"doesn't exist yet: {_short(base.message)}")
    # Not an assertion. Calling it strong is a heuristic: it is right when the
    # old code caused the exception, and wrong when something else differed
    # between the runs, such as a changed data file outside the patterns. So
    # the reason says where it was raised and asks the reviewer to check.
    where = f" {base.location()}" if base.location() else ""
    message = f": {_short(base.message)}" if base.message.strip() else ""
    return STRONG, (f"Fails at base with {base.exc_type}{where}{message}. Not a check, so inspect the cause: "
                    f"this is strong only if the old code caused it.")
