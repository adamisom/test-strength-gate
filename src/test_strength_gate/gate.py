"""The gate itself: find the PR's tests, run them at base and at head, judge them."""

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import gitutil
from .classify import BROKEN_AT_HEAD, INCONCLUSIVE, SKIPPED, WEAK, Outcome, summarize, verdict
from .runner import run_pytest
from .selection import DEFAULT_GLOBS, matches_any, pick_judged


@dataclass
class JudgedTest:
    id: str          # a test ID, or just a file path for a file that fails to import at head
    kind: str        # added or modified
    base: Outcome
    head: Outcome
    verdict: str
    reason: str

    @property
    def is_file(self):
        """A row for a whole test file whose tests could not be judged at head."""
        return "::" not in self.id


@dataclass
class GateResult:
    base: str        # the commit actually used as base (normally the merge-base)
    head: str
    test_files: list = field(default_factory=list)
    source_files: list = field(default_factory=list)  # changed .py files that are not tests
    other_files: list = field(default_factory=list)   # changed or deleted non-Python inputs outside the patterns
    deleted_files: list = field(default_factory=list)  # every file the PR deletes
    tests: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # git-ignored .py files copied from the checkout into the base worktree
    # that the base run imported; they were generated for head
    copied_imported: list = field(default_factory=list)
    # changed packaging files; the base run reads the installed package's
    # metadata (version, entry points), which head's install wrote
    packaging_files: list = field(default_factory=list)
    # the project's distributions whose installed metadata the base run read
    metadata_read: list = field(default_factory=list)

    @property
    def pr_kind(self):
        """'tests_only', 'all_weak' (looks like a refactor), or 'normal'.

        A PR is tests only when it changes no Python source and no other
        non-doc file outside the test patterns, such as a template or a
        compiled extension's source, which can carry behavior too.
        """
        if not self.source_files and not self.other_files:
            return "tests_only"
        judged = [t for t in self.tests if t.verdict not in (SKIPPED, BROKEN_AT_HEAD) and not t.is_file]
        if judged and all(t.verdict == WEAK for t in judged):
            return "all_weak"
        return "normal"


def _set_origin(outcome, globs):
    """Mark whether the exception was raised in test code, project code or a library."""
    if outcome.raised_at:
        path = outcome.raised_at.rsplit(":", 1)[0]
        outcome.origin = ("library" if not outcome.raised_inside
                          else "test" if matches_any(path, globs) else "project")
    return outcome


# Files whose change reaches the installed package's metadata. The action
# installs the project at head, and importlib.metadata reads that install's
# .dist-info in the base run too, whatever sys.path says.
PACKAGING_FILES = {"pyproject.toml", "setup.py", "setup.cfg"}


def _distribution_names(repo, rev):
    """The project's distribution names, from the root packaging files at `rev`."""
    names = set()
    for path, sections, pattern in (("pyproject.toml", ("project", "tool.poetry"), r"name\s*=\s*[\"']([^\"']+)[\"']"),
                                    ("setup.cfg", ("metadata",), r"name\s*=\s*(\S+)")):
        section = None
        for line in (gitutil.show(repo, rev, path) or "").splitlines():
            header = re.match(r"\s*\[([^\]]+)\]", line)
            if header:
                section = header.group(1).strip()
            elif section in sections and (m := re.match(rf"\s*{pattern}", line)):
                names.add(m.group(1))
    setup = gitutil.show(repo, rev, "setup.py") or ""
    names.update(re.findall(r"\bname\s*=\s*[\"']([^\"']+)[\"']", setup))
    return sorted(names)


def _is_docs(path):
    """Files that tests almost never read, left out of the changed-inputs note."""
    return path.endswith((".md", ".rst")) or path.startswith(("docs/", ".github/"))


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
        if status == "D":
            result.deleted_files.append(path)
        if PurePosixPath(path).name in PACKAGING_FILES:
            result.packaging_files.append(path)
        if matches_any(path, globs):
            if status != "D":
                result.test_files.append(path)
        elif path.endswith(".py"):
            result.source_files.append(path)
        elif not _is_docs(path):
            result.other_files.append(path)
    # Test-side files the PR deletes, such as a data fixture, are removed from
    # the base run, just as added and modified ones are copied in.
    deleted_test_files = [p for p in result.deleted_files if matches_any(p, globs)]
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
        missing = gitutil.fill_submodules(repo, head_wt, head_sha) + gitutil.fill_submodules(repo, base_wt, base_sha)
        if missing:
            result.warnings.append("Submodules the checkout has no clone or commit for, so they are empty in the "
                                   "runs (check out with submodules: true): " + ", ".join(sorted(set(missing))))
        generated = gitutil.ignored_python_files(repo)
        copied = gitutil.copy_generated_files(repo, head_wt, generated)
        base_copied = gitutil.copy_generated_files(repo, base_wt, generated)
        copied += base_copied
        if copied:
            result.warnings.append("Copied git-ignored files from the checkout into the worktrees "
                                   "(usually generated at install time): " + ", ".join(sorted(set(copied))))
        head_collect = run(head_wt, targets, collect_only=True)
        base_collect = run(base_wt, base_targets, collect_only=True) if base_targets else None
        # A changed test file that can't be collected at head has no test IDs,
        # so it gets one row of its own instead of silently dropping out.
        # A file that fails to import is broken at head. But when the pytest
        # run as a whole fails (a timeout, an internal error, a conftest.py
        # that won't load), no file's result is known, so those rows are
        # inconclusive.
        rows = {file: (BROKEN_AT_HEAD, "collect",
                       f"The file fails to import at head, so none of its tests can be judged: {message}")
                for file, message in head_collect["collect_errors"].items() if file in targets}
        if head_collect.get("startup_error"):
            for file in targets:
                rows.setdefault(file, (INCONCLUSIVE, "startup",
                                       f"pytest failed as a whole at head, so none of the tests in this file "
                                       f"were judged: {head_collect['startup_error']}"))
        for file, message in head_collect["collect_errors"].items():
            if file not in rows:
                result.warnings.append(f"{file} fails to import at head: {message}")
        for file in sorted(rows):
            label, phase, reason = rows[file]
            kind = "added" if base_source[file] is None else "modified"
            result.tests.append(JudgedTest(file, kind, Outcome("not_run", message="not run"),
                                           Outcome("error", phase, message=reason), label, reason))

        judged = pick_judged(head_collect["items"], base_collect["items"] if base_collect else [],
                             head_source, base_source)
        if not judged:
            return result

        # Base run: base source code plus the PR's version of the test side,
        # with the test files it deletes removed and the ones it changes copied
        # in. Removing first lets a file be replaced by a folder of its name.
        gitutil.remove_files(base_wt, deleted_test_files)
        gitutil.checkout_files(base_wt, head_sha, result.test_files)
        files = sorted({test_id.split("::")[0] for test_id in judged})
        dists = _distribution_names(repo, head_sha) if result.packaging_files else []
        base_run = run(base_wt, files, select=list(judged), watch=base_copied, dists=dists)
        head_run = run(head_wt, files, select=list(judged))

    if head_run["misrouted"]:
        result.warnings.append("The head run loaded project code from outside the head worktree: "
                               + _misrouted_message(head_run["misrouted"]))
    base_items, head_items = set(base_run["items"]), set(head_run["items"])
    result.copied_imported = sorted(base_run.get("copied_imported") or [])
    result.metadata_read = sorted(base_run.get("metadata_read") or [])
    for test_id, kind in judged.items():
        file = test_id.split("::")[0]
        if base_run["misrouted"]:
            # The base run tested the wrong code, so none of its results count.
            base_outcome = Outcome("error", "misrouted", message=_misrouted_message(base_run["misrouted"]))
        else:
            base_outcome = summarize(base_run["results"].get(test_id), base_run["collect_errors"].get(file),
                                     base_run.get("startup_error"), test_id in base_items)
            if base_outcome.missing_path:
                base_outcome.file_at_head = gitutil.exists(repo, head_sha, base_outcome.missing_path)
            _set_origin(base_outcome, globs)
        head_outcome = summarize(head_run["results"].get(test_id), head_run["collect_errors"].get(file),
                                 head_run.get("startup_error"), test_id in head_items)
        label, reason = verdict(base_outcome, head_outcome)
        if label == WEAK and result.copied_imported:
            # The copied file was generated for head, so the pass at base may
            # come from head's values in it rather than from base code.
            label = INCONCLUSIVE
            reason = (f"Passes at base, but the base run imported or read "
                      f"{', '.join(result.copied_imported)}, which the gate copied from the checkout and which was generated "
                      f"for head, so the pass may come from head's values in it.")
        if label == WEAK and result.packaging_files and result.metadata_read:
            # The install was made from head, so the pass at base may come
            # from head's version or entry points rather than from base code.
            label = INCONCLUSIVE
            reason = (f"Passes at base, but the base run read the installed metadata of "
                      f"{', '.join(result.metadata_read)}, which head's install wrote, and the PR changes "
                      f"{', '.join(result.packaging_files)}, so the pass may come from head's metadata.")
        elif label == WEAK and result.packaging_files:
            reason += (f" The PR changes {', '.join(result.packaging_files)}, and installed metadata such as "
                       f"the version and entry points comes from head's install, so check that the test "
                       f"doesn't depend on it.")
        result.tests.append(JudgedTest(test_id, kind, base_outcome, head_outcome, label, reason))
    return result
