"""Markdown and JSON reports."""

import json
from collections import Counter
from dataclasses import asdict

from .classify import BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, STRONG, WEAK


def summary_line(tests):
    # Rows without "::" are test files that fail to import at head.
    files = [t for t in tests if "::" not in t.id]
    tests = [t for t in tests if "::" in t.id]
    unjudged = (f"{len(files)} changed test file{'s' * (len(files) != 1)} "
                f"{'fails' if len(files) == 1 else 'fail'} to import at head, so {'its' if len(files) == 1 else 'their'} "
                f"tests were not judged")
    if not tests:
        return f"No tests could be judged: {unjudged}." if files else "No added or modified tests to judge."
    tail = f". Also, {unjudged}." if files else ""
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

Each test above was run twice, once on the base commit with only the PR's test files copied in, and once on the head commit.

- **STRONG**: fails at base on a check, so it would catch the source change going missing.
- **WEAK**: passes at base. This is a prompt for a reviewer, not a failure. It is expected for refactors and for tests that pin down existing behavior, but for a bug fix or feature it can mean the test doesn't exercise the change.
- **INCONCLUSIVE**: fails at base because the code it calls doesn't exist yet (import, attribute or signature errors), because it reads a file that exists at head but not in the base run, or because setup failed. It shows the API or input is new, not that the behavior is checked.
- **BROKEN_AT_HEAD**: does not pass at head, so nothing else about it can be judged. A row with a file path instead of a test is a changed test file that fails to import at head.
- **SKIPPED**: skipped, so not judged."""


PR_KIND_NOTES = {
    "tests_only": "This PR changes no Python source outside its test files, so its tests are expected "
                  "to pass at base. Weak results are normal here.",
    "all_weak": "Every judged test passes without the source change. That is expected for a refactor, "
                "or for tests that pin down existing behavior. For a bug fix or a new feature, it suggests "
                "the tests don't exercise the change.",
}


def to_markdown(result):
    lines = ["## Test strength gate", "", summary_line(result.tests), ""]
    if result.tests and result.pr_kind in PR_KIND_NOTES:
        lines += [PR_KIND_NOTES[result.pr_kind], ""]
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
