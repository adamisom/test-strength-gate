"""Build small git repos for end-to-end tests."""

import subprocess
import sys
import textwrap
from pathlib import Path

from test_strength_gate.gate import run_gate

# Keep the fixture repos independent of the user's git config (signing, hooks).
GIT = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
       "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


class GitRepo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        return subprocess.run([*GIT, "-C", str(self.path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            file = self.path / name
            if text is None:
                file.unlink()
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(textwrap.dedent(text))

    def commit(self, files, message="change"):
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def gate(self, base, head, **kwargs):
        return run_gate(self.path, base, head, python=sys.executable, **kwargs)
