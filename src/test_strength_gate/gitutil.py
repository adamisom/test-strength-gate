"""Thin wrappers around the git commands the gate needs."""

import os
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
    """File contents at a revision, or None if the file does not exist there.

    Bytes that aren't UTF-8, e.g. in a latin-1 test file, become U+FFFD. The
    gate only parses the text to fingerprint test functions, and both the base
    and the head version are decoded the same way, so the comparison holds.
    """
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
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


# Ignored folders that hold environments or build output, not generated code.
NOT_GENERATED = {".git", ".venv", "venv", "env", ".tox", ".nox", "build", "dist", "node_modules",
                 "__pycache__", ".eggs", ".mypy_cache", ".pytest_cache", "site-packages"}


def _generated_folder(path):
    return not (path.name in NOT_GENERATED or path.name.endswith(".egg-info")
                or (path / "pyvenv.cfg").exists())


def ignored_python_files(repo):
    """Git-ignored .py files in the checkout: {path: folder that must exist in the worktree}.

    Build tools write files such as a package's version.py (hatch-vcs,
    setuptools-scm) into the checkout during `pip install`, and git ignores
    them, so a fresh worktree lacks them and the package fails to import.
    Code generators such as protobuf write a whole ignored folder, e.g.
    pkg/gen/. `--directory` collapses an ignored folder to one entry ending
    in '/', so loose files come back as they are, and the .py files in an
    ignored folder are listed by walking it, skipping folders that hold an
    environment or build output (a virtualenv, build/, node_modules/).
    A loose file needs its own folder in the worktree, and a file in an
    ignored folder needs the folder that holds the ignored one.
    """
    out = git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    found = {}
    for entry in filter(None, out.split("\0")):
        if entry.endswith(".py"):
            found[entry] = str(Path(entry).parent)
        elif entry.endswith("/"):
            top = Path(repo) / entry
            if not _generated_folder(top):
                continue
            anchor = str(Path(entry).parent)
            for folder, dirs, files in os.walk(top):
                dirs[:] = [d for d in dirs if _generated_folder(Path(folder) / d)]
                for name in files:
                    if name.endswith(".py"):
                        found[(Path(folder) / name).relative_to(repo).as_posix()] = anchor
    return found


def copy_generated_files(repo, worktree_path, paths):
    """Copy `paths` ({path: folder that must exist}) from the checkout into the worktree.

    Returns the paths copied. A file whose required folder is not in the
    worktree (it is not tracked at that commit) is left out.
    """
    copied = []
    for rel, anchor in sorted(paths.items()):
        target = Path(worktree_path) / rel
        if (Path(worktree_path) / anchor).is_dir() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(repo) / rel, target)
            copied.append(rel)
    return copied
