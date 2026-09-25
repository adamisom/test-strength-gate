"""Build a toy repo with a one-commit "PR" and run test-strength-gate on it.

Usage (needs git and pytest; the tool itself does not have to be installed):

    python examples/demo.py

The PR fixes a bug in slugify() and adds a word_count() helper. Its three new
tests show the three main verdicts:

    test_slugify_strips_punctuation  fails at base on an assert   -> STRONG
    test_slugify_lowercases          passes at base               -> WEAK
    test_word_count                  imports a function base lacks -> INCONCLUSIVE
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BASE = {
    "textkit.py": """
        def slugify(title):
            return title.lower().replace(" ", "-")
    """,
    "tests/test_textkit.py": """
        from textkit import slugify

        def test_slugify_spaces():
            assert slugify("a b") == "a-b"
    """,
}

PR = {
    "textkit.py": """
        import re

        def slugify(title):
            words = re.findall(r"[a-z0-9]+", title.lower())
            return "-".join(words)

        def word_count(text):
            return len(text.split())
    """,
    "tests/test_textkit.py": """
        from textkit import slugify

        def test_slugify_spaces():
            assert slugify("a b") == "a-b"

        def test_slugify_strips_punctuation():
            assert slugify("Hello, World!") == "hello-world"

        def test_slugify_lowercases():
            assert slugify("ABC") == "abc"

        def test_word_count():
            from textkit import word_count
            assert word_count("one two three") == 3
    """,
}


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                    "-C", str(repo), *args], check=True, capture_output=True)


def commit(repo, files, message):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def main():
    with tempfile.TemporaryDirectory(prefix="tsg-demo-") as tmp:
        repo = Path(tmp) / "toy"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        commit(repo, BASE, "base")
        git(repo, "checkout", "-q", "-b", "pr")
        commit(repo, PR, "Fix slugify punctuation, add word_count")

        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"),
                                                                          os.environ.get("PYTHONPATH")]))}
        cmd = [sys.executable, "-m", "test_strength_gate", "--repo", str(repo), "--base", "main", "--head", "pr"]
        return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
