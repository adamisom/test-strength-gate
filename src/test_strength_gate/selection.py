"""Which files are test files, and which tests the PR added or modified."""

import ast
import re
from pathlib import PurePosixPath

# tests/** also matches data files under tests/, so they travel to base with
# the tests. Only the .py files among them are passed to pytest.
DEFAULT_GLOBS = ["test_*.py", "*_test.py", "tests/**", "**/conftest.py"]


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


# Methods that pytest (or unittest) runs around every test in a class.
_CLASS_SETUP = {"setup_method", "teardown_method", "setup_class", "teardown_class", "setup", "teardown",
                "setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp", "asyncTearDown"}
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _is_autouse_fixture(func):
    return any(isinstance(d, ast.Call) and any(k.arg == "autouse" and isinstance(k.value, ast.Constant)
                                               and k.value.value is True for k in d.keywords)
               for d in func.decorator_list)


def _class_context(cls):
    """The parts of a test class that affect every test in it, as one string.

    That is the class's decorators (e.g. @pytest.mark.usefixtures), bases and
    keywords, its class-level statements (attributes, pytestmark), and its
    setup/teardown methods and autouse fixtures. Other methods, i.e. the tests
    themselves and plain helpers, are left out, so editing one test or a
    helper doesn't make every test in the class count as modified.
    """
    body = [n for n in cls.body
            if not isinstance(n, (*_FUNCTIONS, ast.ClassDef))
            or (isinstance(n, _FUNCTIONS) and (n.name in _CLASS_SETUP or _is_autouse_fixture(n)))]
    if cls.body and _is_docstring(cls.body[0]) and body and body[0] is cls.body[0]:
        body = body[1:]
    return "class " + cls.name + ": " + ", ".join(
        ast.dump(n) for n in [*cls.decorator_list, *cls.bases, *cls.keywords, *body])


def function_fingerprint(source, parts):
    """A formatting-insensitive fingerprint of one test function, or None.

    The fingerprint is ast.dump of the function node, which includes its
    decorators (so a changed @parametrize counts) and ignores comments,
    blank lines and line numbers (so reformatting does not count). The
    function's docstring is dropped too, since editing it doesn't change what
    the test does. For a test method, the context of each enclosing class
    (see _class_context) is included, so a new class decorator or a changed
    setup_method counts as a change to every test in the class.
    """
    if source is None or not parts:
        return None
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return None
    node, context = None, []
    for name in parts:
        node = next((n for n in body if isinstance(n, (ast.ClassDef, *_FUNCTIONS)) and n.name == name), None)
        if node is None:
            return None
        if isinstance(node, ast.ClassDef):
            context.append(_class_context(node))
        body = node.body
    if body and _is_docstring(body[0]):
        node.body = body[1:]
    return "\n".join([*context, ast.dump(node)])


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
