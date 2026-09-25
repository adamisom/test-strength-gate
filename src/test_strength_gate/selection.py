"""Which files are test files, and which tests the PR added or modified."""

import ast
import re
from pathlib import PurePosixPath

DEFAULT_GLOBS = ["test_*.py", "*_test.py", "tests/**/*.py", "**/conftest.py"]


def _glob_to_regex(pattern):
    """Translate a glob with ** into a regex over a full POSIX path.

    `*` stays inside one path segment, `**/` matches zero or more whole
    directories, and a trailing `**` matches anything. fnmatch can't be used
    because its `*` also matches `/`, and PurePath.full_match needs 3.13.
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path, globs):
    """True if `path` matches one of `globs`.

    As in .gitignore, a pattern without a slash matches the file name in any
    directory, and a pattern with a slash matches from the repo root.
    """
    name = PurePosixPath(path).name
    for pattern in globs:
        target = path if "/" in pattern else name
        if _glob_to_regex(pattern).match(target):
            return True
    return False


def split_test_id(test_id):
    """'tests/t.py::TestA::test_b[1-2]' -> ('tests/t.py', ['TestA', 'test_b'])."""
    # Drop the parametrize suffix first; its values may themselves contain "::".
    file, *parts = test_id.split("[", 1)[0].split("::")
    return file, parts


def function_fingerprint(source, parts):
    """A formatting-insensitive fingerprint of one test function, or None.

    The fingerprint is ast.dump of the function node, which includes its
    decorators (so a changed @parametrize counts) and ignores comments,
    blank lines and line numbers (so reformatting does not count). The
    function's docstring is dropped too, since editing it doesn't change what
    the test does.
    """
    if source is None or not parts:
        return None
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return None
    node = None
    for name in parts:
        node = next((n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name), None)
        if node is None:
            return None
        body = node.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        node.body = body[1:]
    return ast.dump(node)


def pick_judged(head_ids, base_ids, head_source, base_source):
    """Return {test_id: 'added' | 'modified'} for tests the PR touched.

    head_source and base_source map a file path to its text at that commit
    (None if absent). A test whose function is unchanged is left out, even
    when it lives in a changed file.
    """
    base_set = set(base_ids)
    judged = {}
    for test_id in head_ids:
        if test_id not in base_set:
            judged[test_id] = "added"
            continue
        file, parts = split_test_id(test_id)
        before = function_fingerprint(base_source.get(file), parts)
        after = function_fingerprint(head_source.get(file), parts)
        if before != after:
            judged[test_id] = "modified"
    return judged
