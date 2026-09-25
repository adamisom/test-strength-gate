"""Command-line entry point."""

import argparse
import os
import shlex
import sys
from pathlib import Path

from .classify import WEAK
from .gate import run_gate
from .gitutil import GitError
from .report import to_json, to_markdown
from .selection import DEFAULT_GLOBS


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="test-strength-gate",
        description="Run a PR's new and changed tests against the base code to see "
                    "whether they would catch the source change going missing.")
    p.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    p.add_argument("--base", required=True, help="base commit (the merge-base with head is used)")
    p.add_argument("--head", required=True, help="head commit")
    p.add_argument("--test-glob", action="append", dest="globs", metavar="PATTERN",
                   help=f"test file pattern, repeatable (default: {' '.join(DEFAULT_GLOBS)})")
    p.add_argument("--pytest-args", default="", help='extra pytest arguments, e.g. "-o addopts="')
    p.add_argument("--python", default=sys.executable,
                   help="Python with pytest and the project's dependencies (default: this one)")
    p.add_argument("--json", type=Path, help="write the JSON report here")
    p.add_argument("--markdown", type=Path, help="write the markdown report here")
    p.add_argument("--fail-on", choices=["weak", "none"], default="none",
                   help="exit 1 if any test is WEAK (default: none, report only)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        result = run_gate(args.repo, args.base, args.head, args.globs,
                          shlex.split(args.pytest_args), args.python)
    except GitError as e:
        print(f"test-strength-gate: {e}", file=sys.stderr)
        return 2

    markdown = to_markdown(result)
    print(markdown)
    if args.markdown:
        args.markdown.write_text(markdown)
    if args.json:
        args.json.write_text(to_json(result))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(markdown)

    weak = [t for t in result.tests if t.verdict == WEAK]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for t in weak:  # shows as a warning on the PR's Files tab
            print(f"::warning file={t.id.split('::')[0]},title=Weak test::"
                  f"{t.id} passes without the source change")
        for t in result.tests:
            if "::" not in t.id:  # a test file that fails to import at head
                print(f"::warning file={t.id},title=Test file not judged::{t.id} fails to import at head")

    # A tests-only PR has no source change to catch, so its weak tests don't fail the gate.
    if args.fail_on == "weak" and weak and result.pr_kind != "tests_only":
        return 1
    return 0
