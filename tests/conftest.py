import pytest

from helpers import GitRepo


@pytest.fixture
def repo(tmp_path):
    return GitRepo(tmp_path / "repo")
