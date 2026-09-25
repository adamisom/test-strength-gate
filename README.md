# test-strength-gate

Status: v0 spike. It works end to end on Python and pytest, and it has been tried on nine merged pull requests from open-source projects.

test-strength-gate checks whether a pull request's new tests would have caught the absence of its source change. It runs the tests the PR added or modified against the old code, with only the PR's test-side changes applied: its added and changed test-side files copied in, and the ones it deletes removed. For each test it reports whether the test fails there (good), passes there (a reviewer should look), or fails for a reason that says little, such as a missing import.

## Why

Many pull requests written by coding agents are merged without a real review, and a green test run is often the only evidence that the change works. A green run shows that the tests pass with the change. It doesn't show that they would fail without it, so a test that asserts almost nothing looks the same as a test that pins down the fix. Running the new tests on the old code is a cheap way to tell the two apart.

## How it works

1. It lists the files that changed between the base commit and the head commit, and keeps the ones that match the test file patterns. It uses the merge-base of the two commits, so changes that landed on the base branch later are not counted.
2. It collects the tests in those files at head and at base, and keeps the tests that are new or whose function changed. It compares functions by their syntax tree, so edits to comments or formatting don't count. For a test method, a change to its class's decorators, base classes, class attributes, setup and teardown methods, or autouse fixtures also counts.
3. It creates a temporary git worktree at base, copies in the head version of every changed file that matches the test patterns, removes the matching files the PR deletes, and runs the selected tests. By default that covers test modules, `conftest.py` files and everything under `tests/`, including data files. A small pytest plugin records, for each test, the phase that failed, the exception type and where it was raised.
4. It runs the same tests in a worktree at head to confirm they pass there, then labels each test and writes a markdown report and a JSON report.

Your own checkout is never modified.

Only files that match the test patterns travel to base, and only those are removed there when the PR deletes them. Any other file the PR adds, changes or deletes, such as a data file or a test helper outside `tests/`, is missing, at its base version, or still present in the base run. A missing file makes a test inconclusive, but a changed one can make a test fail at base for a reason unrelated to the source change, which looks strong. If your tests read such files, add a `--test-glob` pattern for them (see Limits).

## Verdicts

| Verdict | Meaning |
| --- | --- |
| STRONG | The test fails at base on an assertion, on a `pytest.raises` that did not raise, or on another exception that shows base behaves differently. It would catch the source change going missing. For an exception that isn't an assertion, strong is a heuristic (see Limits): the reason says where it was raised and asks the reviewer to check that the old code caused it. For an assertion with no message, the reason shows the failing line and the last error line the test printed to stderr. |
| WEAK | The test passes at base. This is a signal for a reviewer, not a failure. It is expected for refactors and for tests of existing behavior, but for a bug fix or feature it can mean the test doesn't exercise the change. |
| INCONCLUSIVE | The test fails at base only because the code it calls doesn't exist there yet, or because the run itself failed. This covers an ImportError, a NameError, an AttributeError on a module, class or project object, a TypeError about arguments, a fixture error, a file that fails to import at base, and a pytest run that failed as a whole at base or at head (it could not start, timed out, or hit an internal error). A changed test file whose head run failed that way gets one row of its own, with the file path in place of a test ID. It also covers a `FileNotFoundError` for a file that exists at head but not in the base run, such as a data file the PR adds outside the test patterns. When a TypeError about arguments is raised inside the project's own code rather than in the test, the old code may have a real bug, so the reason says the cause is unclear. |
| BROKEN_AT_HEAD | The test does not pass at head, so nothing else about it can be judged. A changed test file that fails to import at head gets one row of its own, with the file path in place of a test ID, and the summary line counts such files separately. |
| SKIPPED | The test was skipped at base or at head. |

The exception rules live in one function, `failure_kind` in `src/test_strength_gate/classify.py`.

When a PR changes no Python source outside its tests, or when every judged test is weak, the report says so once at the top, because weak results are normal for tests-only PRs and refactors.

## Use it as a GitHub Action

The action assumes your workflow has already checked out the repository and installed your project's dependencies and pytest. It installs its own package into the same Python and runs.

Supported runners are Linux and macOS, such as `ubuntu-latest` and `macos-latest`, which is where the tool's own tests run in CI. Windows is not supported. The action's steps use bash, which GitHub's Windows runners also have, but the tool has never run on Windows.

The action itself has not yet run as a step in a real workflow. Its shell step is tested locally against a shallow clone of a merge commit (`tests/test_action.py`), and `examples/action-integration.yml` is a workflow, not yet run, that exercises the action with `uses: ./`.

```yaml
on:
  pull_request:   # never pull_request_target; see examples/workflow.yml

jobs:
  test-strength:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 2
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt pytest
      - uses: OWNER/test-strength-gate@v0
        with:
          fail-on: none
```

On `pull_request`, `actions/checkout` checks out a merge commit. By default the action uses the merge commit's first parent as base and the merge commit itself as head, so it judges exactly what the PR would merge. You can pass `base-sha` and `head-sha` instead, and then you need `fetch-depth: 0`.

| Input | Default | Meaning |
| --- | --- | --- |
| `base-sha` | first parent of the merge commit | base commit |
| `head-sha` | the merge commit | head commit |
| `test-globs` | the tool's list | space-separated test file patterns |
| `pytest-args` | none | extra pytest arguments |
| `fail-on` | `none` | `weak` fails the step when any test is weak, except on tests-only PRs |
| `python` | `python` | interpreter with pytest and your dependencies |
| `path` | `.` | path of the checked-out repository |

The report goes to the job summary, and each weak test gets a warning annotation on the PR. The full example is in `examples/workflow.yml`.

## Use it from the command line

Install it into the environment that has your project and pytest, then point it at two commits.

```sh
pip install /path/to/test-strength-gate
test-strength-gate --repo . --base origin/main --head HEAD
```

```
test-strength-gate --repo PATH --base SHA --head SHA
                   [--test-glob PATTERN ...] [--pytest-args "..."]
                   [--json out.json] [--markdown out.md]
                   [--fail-on weak|none] [--python PATH]
```

It also runs as `python -m test_strength_gate`. The default test patterns are `test_*.py`, `*_test.py`, `tests/**` and `**/conftest.py`, so data files under `tests/` travel with the tests. A pattern without a slash matches a file name in any directory, and a pattern with a slash matches from the repository root.

The exit code is 0, or 1 when you pass `--fail-on weak` and a test is weak, or 2 when git can't resolve the commits. When `GITHUB_STEP_SUMMARY` is set, the markdown report is appended to it.

To see a report in one command, run the demo. It builds a toy repository whose PR has one strong, one weak and one inconclusive test.

```sh
python examples/demo.py
```

## Limits

- **Refactors.** A pure refactor leaves the behavior unchanged, so all its tests pass at base and come out weak. The report notes this, but it can't tell a refactor from a feature whose tests don't test it.
- **Flaky tests.** A flaky test can land in any bucket by chance. v0 runs each test once and doesn't retry.
- **New fixtures and data files.** Files that match the test patterns travel with the tests, and the default `tests/**` covers everything under `tests/`. Files that don't end in `.py` are copied but not passed to pytest. A data file or helper outside the patterns stays at its base version or is missing. When a test fails at base because it reads a file that exists at head but not in the base run, it is inconclusive. A changed or deleted data file outside the patterns is more dangerous, because the test sees the old version at base and can fail there for a reason unrelated to the source change, which looks strong. The report names such files when some verdict is strong. Add a pattern for such files. A changed fixture can also change a test's result for reasons unrelated to the source change, and v0 doesn't judge a test whose only change is in a fixture it uses.
- **Installed packages.** The tool puts each worktree's root and `src/` first on `sys.path`, so an installed or editable copy of your project doesn't hide the base code. Git-ignored `.py` files in the checkout, such as a `version.py` that hatch-vcs or setuptools-scm writes at install time, are copied into both worktrees. If your code lives somewhere else, such as `lib/mypkg/`, the plugin notices when a project module was loaded from outside the worktree, for example from an installed copy in site-packages, and marks the base run inconclusive. It counts as project modules the `.py` files and packages at the root and in `src/`, and any other folder with an `__init__.py` whose parent has none, outside folders such as `tests/`, `docs/`, `examples/` and `vendor/`. A namespace package (one without `__init__.py`) outside the root and `src/` is not recognized, so an installed copy of it could still hide the base code. Compiled extensions are not rebuilt at base.
- **Python and pytest only.** Other languages and test runners are not supported. Tests run from the repository root, and pytest-xdist is untested.
- **Strong from an exception other than an assertion is a heuristic.** Any exception at base that isn't an import, attribute or argument error counts as strong, on the reasoning that the test passes at head, so base behaved differently. That is usually right, but not always. A known counterexample: the PR changes a data file outside the test patterns, the base run reads the old version, and the test fails with a `KeyError` that has nothing to do with the source change. Such a verdict is worded as conditional, and when the PR changes or deletes non-Python files outside the patterns, the report names them at the top.
- **What strong means.** A strong test depends on the change. That doesn't prove it checks the right behavior or checks it thoroughly.

## Prior art

- SWE-bench grades a model's fix with its FAIL_TO_PASS tests, which are the tests that fail before the reference fix and pass after it. This tool applies the same check to the tests in a real pull request.
- pyrite's "verify-red" CI job ([pyrite-wiki/pyrite#357](https://github.com/pyrite-wiki/pyrite/pull/357)) runs nearly the same check inside one repository. test-strength-gate packages the idea as a reusable Action with a per-test report.

## Development

```sh
~/.local/bin/uv venv .venv
~/.local/bin/uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest -q
.venv/bin/python examples/demo.py
```

The tests build small git repositories in temporary directories and run the whole gate against them. `DECISIONS.md` records why the tool works the way it does.
