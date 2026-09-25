"""Markdown and JSON reports."""

import json
from collections import Counter
from dataclasses import asdict

from .classify import BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, STRONG, WEAK


def summary_line(tests):
    if not tests:
        return "No added or modified tests to judge."
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
- **INCONCLUSIVE**: fails at base because the code it calls doesn't exist yet (import, attribute or signature errors), or because setup failed. It shows the API is new, not that the behavior is checked.
- **BROKEN_AT_HEAD**: does not pass at head, so nothing else about it can be judged.
- **SKIPPED**: skipped, so not judged."""


def to_markdown(result):
    lines = ["## Test strength gate", "",
             summary_line(result.tests), "",
             f"Base `{result.base[:10]}` (merge-base), head `{result.head[:10]}`. "
             f"Changed test files: {len(result.test_files)}.", ""]
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
    data["counts"] = dict(Counter(t.verdict for t in result.tests))
    return json.dumps(data, indent=2)
