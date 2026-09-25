"""The gate itself: find the PR's tests, run them at base and at head, judge them."""

import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import gitutil
from .classify import BROKEN_AT_HEAD, SKIPPED, WEAK, Outcome, summarize, verdict
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
    base: str        # the commit actually used as base (normally the merge-base)
    head: str
    test_files: list = field(default_factory=list)
    source_files: list = field(default_factory=list)  # changed .py files that are not tests
    tests: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def pr_kind(self):
        """'tests_only', 'all_weak' (looks like a refactor), or 'normal'."""
        if not self.source_files:
            return "tests_only"
        judged = [t for t in self.tests if t.verdict not in (SKIPPED, BROKEN_AT_HEAD)]
        if judged and all(t.verdict == WEAK for t in judged):
            return "all_weak"
        return "normal"


def _misrouted_message(misrouted):
    return "; ".join(f"`{module}` came from {path}" for module, path in misrouted.items())


def run_gate(repo, base, head, globs=None, pytest_args=(), python="python"):
    repo = Path(repo).resolve()
    globs = globs or DEFAULT_GLOBS
    head_sha = gitutil.resolve(repo, head)
    base_sha = gitutil.resolve(repo, base)
    result = GateResult(base=base_sha, head=head_sha)
    # Judge the PR against the point where it branched off, like GitHub's
    # PR diff. Diffing against the base branch tip would pull in changes
    # that landed on the base branch after the PR was opened.
    try:
        result.base = base_sha = gitutil.merge_base(repo, base_sha, head_sha)
    except gitutil.GitError:
        result.warnings.append("No merge-base found (shallow clone?), so the base commit is used as given.")

    for status, path in gitutil.changed_files(repo, base_sha, head_sha):
        if matches_any(path, globs):
            if status != "D":
                result.test_files.append(path)
        elif path.endswith(".py"):
            result.source_files.append(path)
    # conftest.py files and non-Python files that match the patterns (data
    # fixtures such as tests/**/*.json) are copied to base, but they hold no
    # tests to collect, and pytest exits with "no match" if given one.
    targets = [p for p in result.test_files
               if p.endswith(".py") and PurePosixPath(p).name != "conftest.py"]
    if not targets:
        return result

    head_source = {p: gitutil.show(repo, head_sha, p) for p in targets}
    base_source = {p: gitutil.show(repo, base_sha, p) for p in targets}
    base_targets = [p for p in targets if base_source[p] is not None]

    def run(worktree, files, **kwargs):
        return run_pytest(python, worktree, files, repo, pytest_args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="tsg-") as tmp, \
            gitutil.worktree(repo, head_sha, Path(tmp) / "head") as head_wt, \
            gitutil.worktree(repo, base_sha, Path(tmp) / "base") as base_wt:
        generated = gitutil.ignored_python_files(repo)
        copied = gitutil.copy_generated_files(repo, head_wt, generated)
        copied += gitutil.copy_generated_files(repo, base_wt, generated)
        if copied:
            result.warnings.append("Copied git-ignored files from the checkout into the worktrees "
                                   "(usually generated at install time): " + ", ".join(sorted(set(copied))))
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

    if head_run["misrouted"]:
        result.warnings.append("The head run loaded project code from outside the head worktree: "
                               + _misrouted_message(head_run["misrouted"]))
    for test_id, kind in judged.items():
        file = test_id.split("::")[0]
        if base_run["misrouted"]:
            # The base run tested the wrong code, so none of its results count.
            base_outcome = Outcome("error", "misrouted", message=_misrouted_message(base_run["misrouted"]))
        else:
            base_outcome = summarize(base_run["results"].get(test_id),
                                     base_run["collect_errors"].get(file), base_run.get("startup_error"))
            if base_outcome.missing_path:
                base_outcome.file_at_head = gitutil.show(repo, head_sha, base_outcome.missing_path) is not None
        head_outcome = summarize(head_run["results"].get(test_id),
                                 head_run["collect_errors"].get(file), head_run.get("startup_error"))
        label, reason = verdict(base_outcome, head_outcome)
        result.tests.append(JudgedTest(test_id, kind, base_outcome, head_outcome, label, reason))
    return result
