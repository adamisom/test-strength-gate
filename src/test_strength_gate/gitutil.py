"""Thin wrappers around the git commands the gate needs."""

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve(repo, rev):
    return git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def merge_base(repo, a, b):
    return git(repo, "merge-base", a, b).strip()


def changed_files(repo, base, head):
    """Return [(status, path)] for files that differ between base and head.

    --no-renames turns a rename into a delete plus an add, so a renamed test
    file counts as new, and the gate treats the old path like any deleted file.
    """
    out = git(repo, "diff", "--name-status", "--no-renames", "-z", base, head)
    fields = out.split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def show(repo, rev, path):
    """File contents at a revision, or None if the file does not exist there."""
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def exists(repo, rev, path):
    """True if `path` exists at `rev`. Reads no contents, so binary files are safe."""
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}:{path}"], capture_output=True)
    return proc.returncode == 0


@contextmanager
def worktree(repo, rev, path):
    """A detached worktree at `rev`, removed on exit."""
    git(repo, "worktree", "add", "--detach", "--quiet", str(path), rev)
    try:
        yield Path(path).resolve()
    finally:
        try:
            git(repo, "worktree", "remove", "--force", str(path))
        except GitError:
            git(repo, "worktree", "prune")


def checkout_files(worktree_path, rev, paths):
    if paths:
        git(worktree_path, "checkout", rev, "--", *paths)


def remove_files(worktree_path, paths):
    """Delete `paths` from the worktree, and any folders that are left empty."""
    if paths:
        git(worktree_path, "rm", "-q", "-f", "--ignore-unmatch", "--", *paths)


def ignored_python_files(repo):
    """Git-ignored .py files in the checkout, outside ignored directories.

    Build tools write files such as a package's version.py (hatch-vcs,
    setuptools-scm) into the checkout during `pip install`, and git ignores
    them, so a fresh worktree lacks them and the package fails to import.
    `--directory` collapses an ignored directory (a virtualenv, build/) to a
    single entry ending in '/', so only loose files come back.
    """
    out = git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    return [p for p in out.split("\0") if p.endswith(".py")]


def copy_generated_files(repo, worktree_path, paths):
    """Copy `paths` from the checkout into the worktree where their folder exists there.

    Returns the paths copied. A file whose folder is not in the worktree
    (it is not tracked at that commit) is left out.
    """
    copied = []
    for rel in paths:
        target = Path(worktree_path) / rel
        if target.parent.is_dir() and not target.exists():
            shutil.copy2(Path(repo) / rel, target)
            copied.append(rel)
    return copied
