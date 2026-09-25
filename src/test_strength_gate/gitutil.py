"""Thin wrappers around the git commands the gate needs."""

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
    file counts as new and the old path is ignored like any deleted file.
    """
    out = git(repo, "diff", "--name-status", "--no-renames", "-z", base, head)
    fields = out.split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def show(repo, rev, path):
    """File contents at a revision, or None if the file does not exist there."""
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


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
