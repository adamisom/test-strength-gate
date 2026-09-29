# test-strength-gate

This is version 0. It works end to end on Python and pytest from the command line, and it has been tried on nine merged pull requests from open-source projects. The GitHub Action has run in one real workflow, on a demo pull request.

test-strength-gate checks whether a pull request's new tests would have caught the absence of its source change. It runs the tests the PR added or modified against the old code, with only the PR's test-side changes applied, which means its added and changed test-side files are copied in and the ones it deletes are removed. For each test it reports whether the test fails there (good), passes there (a reviewer should look), or fails for a reason that says little, such as a missing import.

## Why

Many pull requests written by coding agents get no human review. A 2026 study of 33,596 pull requests written by coding agents found that 61.38% had no recorded review ([arXiv 2605.02273](https://arxiv.org/abs/2605.02273)). In that setting, a green test run is often the only evidence that the change works. A green run shows that the tests pass with the change. It doesn't show that they would fail without it, so a test that asserts almost nothing looks the same as a test that pins down the fix. Running the new tests on the old code is a cheap way to tell the two apart.

## Evaluation

In evaluation terms, the gate is an automatic grader for one property of agent output, which is whether the tests in a pull request carry evidence about its source change. It has three main outcomes rather than two, so it can report that it doesn't know instead of forcing a verdict. Its first measurement came with a hand check of every weak result, which checks the grader as well as the tests.

On 30 new or changed tests from 8 merged pull requests written by coding agents, 7 tests passed without the source change. A hand check of those 7 found that 3 never reach the code the PR changed, and the other 4 are weak for an expected reason, such as a control test for behavior that existed before the PR. Of the rest, 20 tests were strong and 3 were inconclusive. The hand check also found all 5 strong verdicts that rest on an exception other than an assertion to be correct.

The sample is small, and a weak test is a prompt for a reviewer rather than proof that a PR is wrong. The per-PR table, the repositories and PR numbers, and the hand check of each weak test are in [docs/evaluation.md](docs/evaluation.md).

## How it works

1. It lists the files that changed between the base commit and the head commit, and keeps the ones that match the test file patterns. It uses the merge-base of the two commits, so changes that landed on the base branch later are not counted.
2. It collects the tests in those files at head and at base, and keeps the tests that are new or whose function changed. It compares functions by their syntax tree, so edits to comments or formatting don't count. For a test method, a change to its class's decorators, base classes, class attributes, setup and teardown methods, or autouse fixtures also counts.
3. It creates a temporary git worktree at base, copies in the head version of every changed file that matches the test patterns, removes the matching files the PR deletes, and runs the selected tests, passing pytest only the files that hold one. By default that covers test modules, `conftest.py` files and everything under `tests/`, including data files. A small pytest plugin records, for each test, the phase that failed, the exception type and where it was raised.
4. It runs the same tests in a worktree at head to confirm they pass there, then labels each test and writes a markdown report and a JSON report.

Your own checkout is never modified.

Only files that match the test patterns travel to base, and only those are removed there when the PR deletes them. Any other file the PR adds, changes or deletes, such as a data file or a test helper outside `tests/`, is missing, at its base version, or still present in the base run. A missing file makes a test inconclusive, but a changed one can make a test fail at base for a reason unrelated to the source change, which looks strong. If your tests read such files, add a `--test-glob` pattern for them (see Limits).

## Verdicts

| Verdict | Meaning |
| --- | --- |
| STRONG | The test fails at base on an assertion, on a `pytest.raises` that did not raise, or on another exception that shows base behaves differently. It would catch the source change going missing. For an exception that isn't an assertion, strong is a heuristic (see Limits): the reason says where it was raised and asks the reviewer to check that the old code caused it. For an assertion with no message, the reason shows the failing line and the last line the test printed to stderr that names an error, or else its last stderr line. |
| WEAK | The test passes at base. This is a signal for a reviewer, not a failure. It is expected for refactors and for tests of existing behavior, but for a bug fix or feature it can mean the test doesn't exercise the change. |
| INCONCLUSIVE | The test fails at base only because the code it calls doesn't exist there yet, or because the run itself failed. This covers an ImportError, a NameError, an AttributeError on a module, a class or any object that isn't a Python builtin, a TypeError about arguments, a fixture error, a file that fails to import at base, a pytest run that failed as a whole at base or at head (it could not start, timed out, or hit an internal error), and a test that pytest did not collect, or collected but did not run, at base or at head. A changed test file whose head run failed that way gets one row of its own, with the file path in place of a test ID. It also covers a `FileNotFoundError` for a file that exists at head but not in the base run, such as a data file the PR adds outside the test patterns. When a TypeError about arguments is raised inside the project's own code rather than in the test, the old code may have a real bug, so the reason says the cause is unclear. |
| BROKEN_AT_HEAD | The test does not pass at head, so nothing else about it can be judged. A changed test file that fails to import at head gets one row of its own, with the file path in place of a test ID, and the summary line counts such files separately. |
| SKIPPED | The test was skipped at base or at head. |

The exception rules live in one function, `failure_kind` in `src/test_strength_gate/classify.py`.

When a PR changes nothing outside its tests and docs, or when every judged test is weak, the report says so once at the top, because weak results are normal for tests-only PRs and refactors.

## Use it as a GitHub Action

The action assumes your workflow has already checked out the repository and installed your project's dependencies and pytest. It installs its own package into the same Python and runs. It installs with pip, or with `uv pip` when that Python has no pip and `uv` is on PATH, as in a virtualenv made by `uv venv`.

Supported runners are Linux and macOS, such as `ubuntu-latest` and `macos-latest`, which is where the tool's own tests run in CI. Windows is not supported. The action's steps use bash, which GitHub's Windows runners also have, but the tool has never run on Windows.

On 9/28/26 the action ran as a step in a real GitHub workflow for the first time, in a small demo repository (adamisom/tsg-demo, private for now) on a pull request with one test of each kind. It gave the expected verdicts, 1 strong, 1 weak and 1 inconclusive, wrote the report to the job summary, and put one "Weak test" warning on the weak test's file. It has not yet run on a pull request from a fork. Its shell step is tested locally against a shallow clone of a merge commit (`tests/test_action.py`), and `examples/action-integration.yml` is a workflow, not yet run, that exercises the action with `uses: ./`.

To install it, add a workflow like this one. Use the `pull_request` trigger only, run it on a Linux or macOS runner, and check out with `fetch-depth: 2`. Pin the action to a full commit sha, or to a release tag once one exists. There is no release tag yet.

```yaml
on:
  pull_request:   # never pull_request_target, see examples/workflow.yml

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
      - uses: adamisom/test-strength-gate@FULL_COMMIT_SHA   # or a release tag
        with:
          fail-on: none
```

On `pull_request`, `actions/checkout` checks out a merge commit. By default the action uses the merge commit's first parent as base and the merge commit itself as head, so it judges exactly what the PR would merge. You can pass both `base-sha` and `head-sha` instead, and then you need `fetch-depth: 0`. Passing only one of them is an error.

| Input | Default | Meaning |
| --- | --- | --- |
| `base-sha` | first parent of the merge commit | base commit, set together with `head-sha` |
| `head-sha` | the merge commit | head commit, set together with `base-sha` |
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
                   [--annotation-path-prefix PATH]
```

It also runs as `python -m test_strength_gate`. The default test patterns are `test_*.py`, `*_test.py`, `tests/**` and `**/conftest.py`, so data files under `tests/` travel with the tests. A pattern without a slash matches a file name in any directory, and a pattern with a slash matches from the repository root.

In a GitHub workflow, `--annotation-path-prefix` is the repository's path from the workspace root, which the action sets from its `path` input, so that each annotation points at the right file.

The exit code is 0, or 1 when you pass `--fail-on weak` and a test is weak, or 2 when git can't resolve the commits or a file path in git's output isn't UTF-8. When `GITHUB_STEP_SUMMARY` is set, the markdown report is appended to it.

To see a report in one command, run the demo. It builds a toy repository whose PR has one strong, one weak and one inconclusive test.

```sh
python examples/demo.py
```

## Limits

- **Refactors.** A pure refactor leaves the behavior unchanged, so all its tests pass at base and come out weak. The report notes this, but it can't tell a refactor from a feature whose tests don't test it.
- **Flaky tests.** A flaky test can land in any bucket by chance. v0 runs each test once and doesn't retry.
- **New fixtures and data files.** Files that match the test patterns travel with the tests, and the default `tests/**` covers everything under `tests/`. Files that don't end in `.py` are copied but not passed to pytest. A data file or helper outside the patterns stays at its base version or is missing. When a test fails at base because it reads a file that exists at head but not in the base run, it is inconclusive. A changed or deleted data file outside the patterns is more dangerous, because the test sees the old version at base and can fail there for a reason unrelated to the source change, which looks strong. The report names such files when some verdict is strong. Add a pattern for such files. A changed fixture can also change a test's result for reasons unrelated to the source change, and v0 doesn't judge a test whose only change is in a fixture it uses. The same goes for a test whose only change is in module-level code of its file, such as a constant, a helper function or `pytestmark`, so a PR that changes what a test expects through a module-level constant can get the report "No added or modified tests to judge".
- **Source modules named like tests.** A pattern without a slash matches a file name in any folder, so a source module inside a package whose name matches `test_*.py` or `*_test.py`, such as `pkg/test_utils.py`, is treated as test side, and the base run gets its head version. A test that depends on a change to such a module then comes out weak with no warning, and the PR can be labeled tests only. If your project has such modules, pass a narrower `--test-glob`, e.g. `tests/**` and `**/conftest.py`.
- **Installed packages.** The tool puts each worktree's root and `src/` first on `sys.path`, so an installed or editable copy of your project doesn't hide the base code. Git-ignored `.py` files in the checkout, such as a `version.py` that hatch-vcs or setuptools-scm writes at install time, are copied into both worktrees. Such a file was generated for head, so a test that checks a value in it, such as the version, can pass at base and look weak. When the base run imports a copied file, the report names it at the top and every weak verdict's reason says so. If your code lives somewhere else, such as `lib/mypkg/`, the plugin notices when a project module was loaded from outside the worktree, for example from an installed copy in site-packages, and marks the base run inconclusive. It counts as project modules the `.py` files and packages at the root and in `src/`, and any other folder with an `__init__.py` whose parent has none, outside folders such as `tests/`, `docs/`, `examples/` and `vendor/`. A namespace package (one without `__init__.py`) outside the root and `src/` is not recognized, so an installed copy of it could still hide the base code. Compiled extensions are not rebuilt at base, so when a PR's source change is in a compiled extension, the weak verdicts for its tests mean nothing.
- **Python and pytest only.** Other languages and test runners are not supported. Tests run from the repository root. A project with pytest-xdist's `-n` in its `addopts` works, and the tool's tests cover that case, including the check for project code loaded from outside the worktree.
- **Strong from an exception other than an assertion is a heuristic.** Any exception at base that isn't an import, attribute or argument error counts as strong, on the reasoning that the test passes at head, so base behaved differently. That is usually right, but not always. In one known counterexample, the PR changes a data file outside the test patterns, and the base run reads the old version. The test then fails at base with a `KeyError` that has nothing to do with the source change. Such a verdict is worded as conditional, and when the PR changes or deletes non-Python files outside the patterns, the report names them at the top. A missing API that shows up as some other failure also comes out strong, although the test never saw the old code behave. Four known forms are listed here.
  - argparse exits with `SystemExit` because the base code doesn't know a new command-line option.
  - A pydantic-style model rejects a new field with "Extra inputs are not permitted".
  - A registry dictionary raises `KeyError` for a key the PR adds.
  - The test checks `assert hasattr(module, "new_func")`, which fails at base on an assertion, with the firm wording.
- **A test of a new helper can't show that the helper is wired in.** When a PR adds a helper and changes other code to call it, a test that calls the helper directly is inconclusive at base, and the report can't say whether any test covers the new call. In verifiers #836, three tests call a new helper, and no test checks the display code that the PR changed to call it.
- **What strong means.** A strong test depends on the change. That doesn't prove it checks the right behavior or checks it thoroughly.

## Prior art

The idea is not new. Running a change's new tests against the code from before the change is how several existing tools and projects decide whether a test carries evidence.

- SWE-bench grades a model's fix with its FAIL_TO_PASS tests, which are the tests that fail before the reference fix and pass after it. This tool applies the same check to the tests in a real pull request.
- pyrite's "verify-red" CI job ([pyrite-wiki/pyrite#357](https://github.com/pyrite-wiki/pyrite/pull/357)) runs nearly the same check for pytest inside one repository. Its review thread lists the traps it hit, and this tool handles each of them.
- Yosemite-Crew's "Tests Must Be Able To Fail" check ([script](https://github.com/YosemiteCrew/Yosemite-Crew/blob/main/scripts/ci/tests-must-be-able-to-fail.mjs)) does the same for JavaScript and jest. Its [issue #3530](https://github.com/YosemiteCrew/Yosemite-Crew/issues/3530) describes a false result from trusting the pull request's base sha, which is why this Action takes its base from the merge commit.

The contribution here is the packaging and the handling of edge cases. The packaging is a reusable Action with a per-test report. The edge cases are the ones that fool a simple version, e.g., a test that fails at base only because it imports a function the PR adds. I built a first version of this check at a previous job, and this repository is a new implementation.

## Development

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python -m pytest -q
.venv/bin/python examples/demo.py
```

The tests build small git repositories in temporary directories and run the whole gate against them. `DECISIONS.md` records why the tool works the way it does.

## License

MIT. See `LICENSE`.
