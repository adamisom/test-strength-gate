"""Markdown and JSON reports."""

import json
from collections import Counter
from dataclasses import asdict

from .classify import BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, STRONG, WEAK


def summary_line(tests):
    broken = sum(1 for t in tests if t.is_file and t.verdict == BROKEN_AT_HEAD)
    run_failed = sum(1 for t in tests if t.is_file and t.verdict != BROKEN_AT_HEAD)
    tests = [t for t in tests if not t.is_file]

    def files(n):
        return f"{n} changed test file{'s' * (n != 1)}"
    parts = []
    if broken:
        parts.append(f"{files(broken)} {'fails' if broken == 1 else 'fail'} to import at head")
    if run_failed:
        parts.append(f"pytest failed as a whole at head for {files(run_failed)}")
    unjudged = (", and ".join(parts)
                + f", so {'its' if broken + run_failed == 1 else 'their'} tests were not judged")
    if not tests:
        return f"No tests could be judged: {unjudged}." if parts else "No added or modified tests to judge."
    tail = f". Also, {unjudged}." if parts else ""
    return _tests_summary(tests) + tail


def _tests_summary(tests):
    kinds = Counter(t.kind for t in tests)
    counts = Counter(t.verdict for t in tests)
    n = len(tests)
    if kinds["modified"]:
        head = f"{n} new or changed test{'s' * (n != 1)} ({kinds['added']} new, {kinds['modified']} modified)"
    else:
        head = f"{n} new test{'s' * (n != 1)}"
    weak = counts[WEAK]
    parts = [f"{counts[STRONG]} strong",
             f"{weak} weak ({'passes' if weak == 1 else 'pass'} without the source change)",
             f"{counts[INCONCLUSIVE]} inconclusive"]
    if counts[BROKEN_AT_HEAD]:
        parts.append(f"{counts[BROKEN_AT_HEAD]} broken at head")
    if counts[SKIPPED]:
        parts.append(f"{counts[SKIPPED]} skipped")
    return f"{head}: {', '.join(parts)}"


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


LEGEND = """\
### How to read this

Each judged test was run twice, once on the base commit with only the PR's test-side changes applied (its test files copied in, and the ones it deletes removed), and once on the head commit. A row with a file path instead of a test is a whole changed test file whose tests could not be judged, and none of them ran at base: BROKEN_AT_HEAD if the file fails to import at head, INCONCLUSIVE if the pytest run at head failed as a whole.

- **STRONG**: fails at base, so it would catch the source change going missing. A failed check is firm evidence. Another exception counts only if the old code caused it, so its reason says where it was raised and asks you to inspect the cause.
- **WEAK**: passes at base. This is a prompt for a reviewer, not a failure. It is expected for refactors and for tests that pin down existing behavior, but for a bug fix or feature it can mean the test doesn't exercise the change.
- **INCONCLUSIVE**: fails at base because the code it calls doesn't exist yet (import, attribute or signature errors), because it reads a file that exists at head but not in the base run, or because setup failed. It shows the API or input is new, not that the behavior is checked. It is also used when a pytest run fails as a whole (a timeout or an internal error), or when a test was not collected or not run at base or at head, since the result is then unknown. A test that passes at base is inconclusive too when the base run imported a git-ignored file the gate copied from the checkout, since that file was generated for head, or when the PR changes packaging files and the base run read the project's installed metadata, which head's install wrote.
- **BROKEN_AT_HEAD**: does not pass at head, so nothing else about it can be judged.
- **SKIPPED**: skipped, so not judged."""


PR_KIND_NOTES = {
    "tests_only": "This PR changes no source outside its test files and docs, so its tests are expected "
                  "to pass at base. Weak results are normal here.",
    "all_weak": "Every judged test passes without the source change. That is expected for a refactor, "
                "or for tests that pin down existing behavior. For a bug fix or a new feature, it suggests "
                "the tests don't exercise the change.",
}


def _other_files_note(paths, deleted, limit=10):
    shown = ", ".join(f"`{_cell(p)}`" + (" (deleted)" if p in deleted else "") for p in paths[:limit])
    if len(paths) > limit:
        shown += f" and {len(paths) - limit} more"
    n = len(paths)
    gone = sum(1 for p in paths if p in deleted)
    if not gone:
        verb, base_run = "changes", f"used {'their' if n != 1 else 'its'} old version"
    elif gone == n:
        verb, base_run = "deletes", f"still had {'them' if n != 1 else 'it'}"
    else:
        verb, base_run = "changes or deletes", "used their old versions and still had the deleted ones"
    return (f"**Check these inputs before trusting a strong verdict.** The PR {verb} {n} "
            f"non-Python file{'s' * (n != 1)} outside the test patterns: {shown}. The base run {base_run}. "
            f"A test that reads one of them can fail at base for a reason unrelated to the source change "
            f"and still come out strong. If they are test data, add a --test-glob that matches them.")


def _copied_imported_note(paths):
    shown = ", ".join(f"`{p}`" for p in paths)
    return (f"**The base run imported files generated for head.** The gate copied {shown} from the "
            f"checkout, where they are git-ignored and usually written at install time, and the base run "
            f"imported or read them. A test that checks a value in them can pass at "
            f"base for that reason, so tests that pass at base are inconclusive here rather than weak.")


def _packaging_note(paths):
    shown = ", ".join(f"`{_cell(p)}`" for p in paths)
    return (f"**Check weak verdicts against the installed metadata.** The PR changes {shown}. The base run "
            f"reads the installed package's metadata, such as its version and entry points, and that install "
            f"was made from head, so a test that checks them can pass at base and look weak.")


def _metadata_read_note(names, paths):
    shown = ", ".join(f"`{_cell(p)}`" for p in paths)
    return (f"**The base run read metadata from head's install.** The PR changes {shown}, and the base run "
            f"read the installed metadata of {', '.join(f'`{n}`' for n in names)}, such as its version or "
            f"entry points, which the install made from head wrote. A test that checks them can pass at base "
            f"for that reason, so tests that pass at base are inconclusive here rather than weak.")


def to_markdown(result):
    lines = ["## Test strength gate", "", summary_line(result.tests), ""]
    if result.tests and result.pr_kind in PR_KIND_NOTES:
        lines += [PR_KIND_NOTES[result.pr_kind], ""]
    if result.other_files and any(t.verdict == STRONG for t in result.tests):
        lines += [_other_files_note(result.other_files, set(result.deleted_files)), ""]
    if result.copied_imported and any(t.base.status == "passed" for t in result.tests):
        lines += [_copied_imported_note(result.copied_imported), ""]
    if result.packaging_files and result.metadata_read and any(t.base.status == "passed" for t in result.tests):
        lines += [_metadata_read_note(result.metadata_read, result.packaging_files), ""]
    elif result.packaging_files and any(t.verdict == WEAK for t in result.tests):
        lines += [_packaging_note(result.packaging_files), ""]
    lines += [f"Base `{result.base[:10]}`, head `{result.head[:10]}`. Changed test files: "
              f"{len(result.test_files)}. Changed Python source files: {len(result.source_files)}.", ""]
    if result.tests:
        lines += ["| Test | Kind | Base | Head | Verdict | Reason |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for t in result.tests:
            lines.append(f"| `{_cell(t.id)}` | {t.kind} | {_cell(t.base.describe())} | "
                         f"{_cell(t.head.describe())} | **{t.verdict}** | {_cell(t.reason)} |")
        lines += ["", LEGEND]
    if result.warnings:
        lines += ["", "### Warnings", ""] + [f"- {_cell(w)}" for w in result.warnings]
    return "\n".join(lines) + "\n"


def to_json(result):
    data = asdict(result)
    data["summary"] = summary_line(result.tests)
    data["pr_kind"] = result.pr_kind
    data["counts"] = dict(Counter(t.verdict for t in result.tests))
    return json.dumps(data, indent=2)
