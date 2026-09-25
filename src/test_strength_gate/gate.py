"""The gate itself: find the PR's tests, run them at base and at head, judge them."""

import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import gitutil
from .classify import summarize, verdict, Outcome
from .runner import run_pytest
from .selection import DEFAULT_GLOBS, matches_any, pick_judged


@dataclass
class JudgedTest:
    id: str
    kind: str        # added or modified
    base: Outcome
    head: Outcome
    verdict: str
    reason: str


@dataclass
class GateResult:
    base: str        # the merge-base actually used
    head: str
    test_files: list = field(default_factory=list)
    tests: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def run_gate(repo, base, head, globs=None, pytest_args=(), python="python"):
    repo = Path(repo).resolve()
    globs = globs or DEFAULT_GLOBS
    head_sha = gitutil.resolve(repo, head)
    # Judge the PR against the point where it branched off, like GitHub's
    # PR diff. Diffing against the base branch tip would pull in changes
    # that landed on the base branch after the PR was opened.
    base_sha = gitutil.merge_base(repo, gitutil.resolve(repo, base), head_sha)
    result = GateResult(base=base_sha, head=head_sha)

    result.test_files = [path for status, path in gitutil.changed_files(repo, base_sha, head_sha)
                         if status != "D" and matches_any(path, globs)]
    # conftest.py files are copied to base, but they hold no tests to collect.
    targets = [p for p in result.test_files if PurePosixPath(p).name != "conftest.py"]
    if not targets:
        return result

    head_source = {p: gitutil.show(repo, head_sha, p) for p in targets}
    base_source = {p: gitutil.show(repo, base_sha, p) for p in targets}
    base_targets = [p for p in targets if base_source[p] is not None]

    def run(worktree, files, **kwargs):
        data = run_pytest(python, worktree, files, repo, pytest_args, **kwargs)
        for module in data["leaked_imports"]:
            result.warnings.append(
                f"`{module}` was imported from the original checkout, not the worktree, "
                "so results may reflect the wrong code (an editable install?).")
        return data

    with tempfile.TemporaryDirectory(prefix="tsg-") as tmp, \
            gitutil.worktree(repo, head_sha, Path(tmp) / "head") as head_wt, \
            gitutil.worktree(repo, base_sha, Path(tmp) / "base") as base_wt:
        head_collect = run(head_wt, targets, collect_only=True)
        base_collect = run(base_wt, base_targets, collect_only=True) if base_targets else None
        for file, message in head_collect["collect_errors"].items():
            result.warnings.append(f"{file} fails to import at head, so its tests were not judged: {message}")
        if head_collect.get("startup_error"):
            result.warnings.append(f"pytest could not collect at head: {head_collect['startup_error']}")

        judged = pick_judged(head_collect["items"], base_collect["items"] if base_collect else [],
                             head_source, base_source)
        if not judged:
            return result

        # Base run: base source code plus the PR's version of every test file.
        gitutil.checkout_files(base_wt, head_sha, result.test_files)
        files = sorted({test_id.split("::")[0] for test_id in judged})
        base_run = run(base_wt, files, select=list(judged))
        head_run = run(head_wt, files, select=list(judged))

    for test_id, kind in judged.items():
        file = test_id.split("::")[0]
        base_outcome = summarize(base_run["results"].get(test_id),
                                 base_run["collect_errors"].get(file), base_run.get("startup_error"))
        head_outcome = summarize(head_run["results"].get(test_id),
                                 head_run["collect_errors"].get(file), head_run.get("startup_error"))
        label, reason = verdict(base_outcome, head_outcome)
        result.tests.append(JudgedTest(test_id, kind, base_outcome, head_outcome, label, reason))
    result.warnings = list(dict.fromkeys(result.warnings))
    return result
