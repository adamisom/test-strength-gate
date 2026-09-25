# Decisions

This is a running log of the engineering decisions behind the v0 spike. Each entry gives the context, the choice, the alternatives, and the reason. The source of each decision is one of these:

- **Brief** means Adam set it before the build started.
- **Build** means I made it while writing the code.
- **Changed** means an earlier decision turned out to be wrong, and the entry says what changed and what the evidence was.

## 1. Python and pytest only, with a standard-library tool

Source: brief.

- **Context.** The question the tool answers applies to any language, but each test runner reports failures differently.
- **Choice.** v0 supports Python and pytest only. The tool uses only the standard library, and pytest is needed only in the environment under test.
- **Alternatives.** A runner-agnostic design that reads JUnit XML, or supporting jest at the same time.
- **Why.** One runner done properly shows whether the idea works. A tool with no dependencies can be pip-installed into any project's environment without version conflicts.

## 2. Temporary git worktrees for both base and head

Source: brief for base, build for head.

- **Context.** The base run needs the base source with the head test files, and the head run needs head.
- **Choice.** The tool creates a detached worktree for base and another for head in a temporary directory, and removes both when it finishes. It never touches the caller's checkout.
- **Alternatives.** Run the head tests in the caller's checkout, or stash, check out base, and restore.
- **Why.** The caller's checkout may have uncommitted changes, and in CI later steps expect it untouched. Worktrees share the object store, so creating one is fast. Using a worktree for head too means both runs happen under the same conditions, i.e., same paths and no local edits.

## 3. Finding test files with the git diff and glob patterns

Source: brief, with details from the build.

- **Context.** The tool needs the list of test files the PR changed.
- **Choice.** It runs `git diff --name-status --no-renames -z` and keeps the paths that match the patterns. Deleted files are ignored. A pattern without a slash matches the file name in any directory, and a pattern with a slash matches from the repository root, as in `.gitignore`. The glob matcher is a small function that turns `*`, `**/` and `?` into a regular expression.
- **Alternatives.** `fnmatch`, or `PurePath.full_match`.
- **Why.** `fnmatch` lets `*` match `/`, so `tests/*.py` would also match files in subdirectories, and `full_match` needs Python 3.13. With `--no-renames`, a renamed test file is a delete plus an add, so its tests count as new, and the tool doesn't need extra code for renames. `-z` keeps paths with spaces intact.

## 4. Judging against the merge-base

Source: build.

- **Context.** In GitHub's pull request event, `base.sha` is the base branch tip when the event fired, not where the PR branched off. If other PRs merge into main after the branch point, a plain diff between that tip and the PR head also shows their changes, reversed.
- **Choice.** The CLI computes `git merge-base base head` and uses it as the real base, as GitHub's own PR diff does. If there is no merge-base, for example in a shallow clone, it uses the base as given and adds a warning.
- **Alternatives.** Trust the base the caller passes.
- **Why.** Without it, a test file that changed on main would look like part of the PR. There is a test for this (`test_uses_merge_base_not_base_branch_tip`).

## 5. The Action defaults to the merge commit's parents

Source: changed after research.

- **Before.** The brief had the action default to `github.event.pull_request.base.sha` and `head.sha`, with `fetch-depth: 0`.
- **Evidence.** On `pull_request`, `actions/checkout` checks out `refs/pull/N/merge`, a merge commit that GitHub rebuilds when the base branch moves. `base.sha` can be stale, and the Yosemite-Crew project counted tests from main as the PR's own because of this ([issue 3530](https://github.com/YosemiteCrew/Yosemite-Crew/issues/3530)).
- **Choice.** When `base-sha` and `head-sha` are empty, the action uses `HEAD^1` (the base branch the merge was built on) as base and `HEAD` (the merge commit) as head, with `fetch-depth: 2`. It warns if `HEAD^2` differs from the PR head in the event. Both inputs can still be set by hand.
- **Alternatives.** Use `HEAD^2` (the PR head) as head, or keep `fetch-depth: 0` with the merge-base. The research note recommended `HEAD^1` as base and the merge commit as head, which is the choice made here.
- **Why.** Using the merge commit as head tests exactly the code that would land, and the diff from `HEAD^1` to `HEAD` is exactly the PR's change on top of current main. With `HEAD^2` as head, the diff would include main's newer changes, reversed. Depth 2 fetches three commits instead of the whole history. Since `HEAD^1` is an ancestor of `HEAD`, the merge-base step from decision 4 still works in the shallow clone, and there is a test that clones a merge commit with `--depth 2`.

## 6. Telling added tests from modified tests

Source: brief, refined in the build.

- **Context.** A PR can add tests and edit existing ones, and an edited test keeps its node ID.
- **Choice.** The tool collects test IDs at head, and at base in a clean base worktree. An ID that exists only at head is added. An ID that exists in both is modified when its function changed. To compare functions, the tool parses each version with `ast` and compares `ast.dump` of the function node, found by class and function name, with any `[param]` suffix removed from the ID first.
- **Alternatives.** Compare the raw function source text, or map diff hunks onto line ranges.
- **Why.** `ast.dump` includes decorators, so a changed `@pytest.mark.parametrize` counts as a change. It ignores comments, blank lines and formatting, so a test that was only reformatted is not judged, and the scenario test checks this. Diff hunks would flag whitespace edits and need care with decorators and moved code. The brief said to compare function source, and `ast.dump` is the same idea without the formatting noise.
- **Surprise.** Adding one case to a parametrize list changes the decorator, so the existing cases count as modified and are judged again. They usually pass at base and come out weak. I kept this because the alternative, comparing each case's values, is much more work, and the report is advisory. A test pins the behavior (`test_f_new_parametrize_case_marks_old_cases_modified`).
- **Known gap.** A test method inherited from a base class can't be found by name in the AST, so if it's unchanged the tool skips it.

## 7. Test IDs relative to the worktree, not pytest's node IDs

Source: build.

- **Context.** pytest node IDs are relative to its rootdir, and the rootdir depends on ini files and on the arguments. The same test could get different IDs in different runs, and the IDs might not be valid paths from the repository root.
- **Choice.** The plugin rewrites every ID as the file path relative to the worktree root, followed by the rest of the node ID.
- **Alternatives.** Force `--rootdir`, which can break projects whose config lives in a subdirectory.
- **Why.** These IDs are the same in the base and head worktrees, match the paths git reports, and are valid pytest arguments from the worktree root.

## 8. Run test files and filter inside the plugin, not node IDs

Source: changed during the build, then confirmed by research.

- **Before.** The brief said to run pytest on the judged node IDs.
- **Evidence.** In a quick experiment with pytest 9.1.1, passing one node ID that doesn't exist made pytest print `ERROR: not found`, exit with code 4, and run none of the other tests. An ID can be missing at base when its parameter values come from source code the PR changed, e.g., a list of enum members. The research note found the same, and also that one collection error stops the whole session with exit code 2 unless `--continue-on-collection-errors` is passed.
- **Choice.** The tool passes the changed test files to pytest with `--continue-on-collection-errors`, and the plugin reads the wanted IDs from a file and deselects every other test in `pytest_collection_modifyitems`. A wanted test that never shows up at base is inconclusive, with the reason "not collected at base".
- **Alternatives.** Pass node IDs and retry file by file when pytest exits with code 4.
- **Why.** One missing test can't hide the results of the others, and only the judged tests run.

## 9. A small recorder plugin instead of JUnit XML

Source: brief, refined in the build.

- **Context.** Classification needs the phase (setup, call, teardown) and the exception type of each failure.
- **Choice.** `tsg_recorder.py` is a pytest plugin loaded with `-p tsg_recorder`. The tool puts the plugin's directory on `PYTHONPATH`. The plugin reads `call.excinfo` in `pytest_runtest_makereport` and writes JSON at the end of the session. The collect-only runs use the same plugin, so the tool reads collected IDs from JSON instead of parsing the `-q` output.
- **Alternatives.** JUnit XML, pytest-json-report, or parsing the terminal output.
- **Why.** The research note showed that JUnit XML writes the same `<failure>` element for an AssertionError and an AttributeError, so it can't support the rules. pytest-json-report hasn't had a release since 2022. The plugin is a plain module on `PYTHONPATH` rather than an import from the installed package, so it works even when the tool runs from a different virtualenv than the project (the `--python` option). The plugin attaches its data to `report.user_properties` so it should reach the main process under pytest-xdist, but I haven't tested xdist.

## 10. Classification rules

Source: brief, widened during the build and after research.

- **Context.** A test that fails at base can fail because it checks behavior, or because the API it calls doesn't exist yet. Only the first kind shows the test would catch the change going missing.
- **Choice.** `failure_kind` in `classify.py` holds all the exception rules in one place.
  - AssertionError and pytest's `Failed` are strong.
  - ImportError, ModuleNotFoundError and NameError are inconclusive.
  - A TypeError whose message matches the signature patterns, such as "unexpected keyword argument", is inconclusive.
  - AttributeError depends on the object, as decision 11 explains.
  - Any other exception, such as a ValueError or KeyError, is strong, and the reason says it was not an assertion.
  - A FileNotFoundError for a file that exists at head is inconclusive. Decision 24 added this.
  - A setup error, a teardown error, a collection error, and a failed run are all inconclusive.
- **Surprise.** A `pytest.raises` block that doesn't raise fails with `Failed: DID NOT RAISE`, and `Failed` is not a subclass of AssertionError, so the brief's rule would have missed a common kind of strong test. `pytest.fail()` raises the same exception.
- **Alternatives.** Count only AssertionError as strong, and call other exceptions inconclusive.
- **Why.** A test that gets a KeyError from the old code has still seen the old code behave differently, and that is the question the tool asks.

## 11. AttributeError is split by what lacked the attribute

Source: changed after research.

- **Before.** The brief listed every AttributeError as inconclusive.
- **Evidence.** `calc.new_func()` on old code raises AttributeError on a module, which means the API is missing. But `find_word(...).upper()` raises AttributeError on `None` when the old code returns `None`, which is a real difference in behavior. Since Python 3.10, an AttributeError from a failed lookup carries `.name` and `.obj`, and I checked this on 3.10 and 3.13.
- **Choice.** The plugin records whether the object was a module, a class, an instance of a builtin type (such as `None`, `str` or `dict`), or some other object. An AttributeError on a builtin value is strong, and everything else is inconclusive. When `.name` is unset, the error was raised by hand, as `monkeypatch` and `mock.patch` do, and it counts as inconclusive.
- **Alternatives.** The research note suggested counting only modules and classes as missing APIs. I also count instances of project classes, because `thing.new_method()` on old code is the most common missing API in practice. pyrite goes further and checks whether the missing name is one the PR adds, which is more precise but needs a diff of every identifier.
- **Why.** It fixes the common false inconclusive (a function that returns `None`) without inventing a strong result for a missing method.

## 12. The head run can override everything

Source: brief, with one change in the build.

- **Context.** A test that fails at base and also fails at head is broken, not strong.
- **Choice.** Any test that doesn't pass at head is BROKEN_AT_HEAD, whatever happened at base. The exception is a test skipped at head, which is SKIPPED, because a skip (for example on the wrong platform) is not a failure.
- **Alternatives.** Trust the caller's own test job to catch head failures.
- **Why.** The head run costs little, since it runs only the judged tests, and it also catches tests that depend on something the base worktree lacks.

## 13. A conftest.py that fails to import makes the whole run inconclusive

Source: build, confirmed by research.

- **Context.** If the PR's `conftest.py` imports a module that only exists at head, the base run fails before any test is collected.
- **Surprise.** A conftest.py in a directory on the command line is loaded when pytest starts, and an ImportError there makes pytest exit with code 4 before the session starts. So the plugin never writes its JSON and there are no per-test results.
- **Choice.** When the JSON is missing, or pytest exits with code 2, 3 or 4 and nothing was recorded, the runner takes the last `E` line of pytest's output as the reason, and every test in that run is inconclusive with it. A per-run timeout of 900 seconds does the same for a hung run.
- **Why.** The reason tells the reviewer exactly what happened, e.g., "pytest could not start at base: ModuleNotFoundError: No module named 'newhelpers'".

## 14. Making sure the base run really tests the base code

Source: build, extended after research.

- **Context.** Projects usually install themselves into the environment, often with `pip install -e .`. An editable install points at the real checkout, which is at head, and a regular install puts a copy of head in site-packages. Either way, the base run could import head code, and every test would look weak with no sign of the problem.
- **Choice.** The runner puts the worktree root and `src/` at the front of `PYTHONPATH`, which comes before site-packages and before the finders that editable installs add. It also sets `PYTHONDONTWRITEBYTECODE=1` so stale `.pyc` files can't survive a source swap. At the end of each run, the plugin lists the project modules that loaded from outside the worktree. A project module is one whose name exists at the worktree root or in `src/`, or any module loaded from the real checkout outside its virtualenv. If the base run has any, all its results become inconclusive, and the reason names the module and the path. For the head run it's only a warning.
- **Alternatives.** Check with `importlib.util.find_spec` before running, as pyrite does, or require users not to install the project.
- **Why.** The path fix covers the two usual layouts, and the check catches the rest instead of reporting false weak results. A test puts the real checkout on `PYTHONPATH` with the package in `lib/`, which is what an editable install does, and checks that the base run is marked inconclusive.

## 15. Tests-only PRs and refactor-like PRs get their own note

Source: changed after research.

- **Before.** The brief exits with code 1 under `--fail-on weak` whenever any test is weak.
- **Evidence.** A PR that only adds tests for existing behavior has no source change to catch, so all its tests pass at base by design. With the brief's rule, `--fail-on weak` would always fail such a PR.
- **Choice.** The tool counts changed Python files that are not tests. With none, the PR is tests-only. The report says weak results are normal, and `--fail-on weak` doesn't fail it. When source changed and every judged test is weak, the report says once that this is expected for a refactor and suspicious for a feature.
- **Alternatives.** A PR label to opt out, as Yosemite-Crew does, or a `control` marker on tests, as pyrite does.
- **Why.** One sentence at the top reads better than a table of identical warnings, and it needs nothing from the PR author.

## 16. Report wording treats weak as a signal

Source: brief.

- **Context.** Refactors and tests of existing behavior pass at base for good reasons.
- **Choice.** The default is `--fail-on none`. The weak reason says "Passes without the source change, so check that it exercises the change", and the legend says outright that weak is not a failure. Each row shows the base and head outcomes with the exception type, so a reviewer can check the label. File paths are trimmed from ImportError messages to keep rows short.
- **Why.** A gate that blocks correct refactors gets turned off. Advisory output that shows its evidence is more likely to be read.

## 17. A composite action that passes inputs through env variables

Source: brief, with details from the build.

- **Choice.** `action.yml` is a composite action. It pip-installs the package from `github.action_path` into the caller's Python and runs the CLI. Every input reaches the script through an env variable, and the script never contains `${{ inputs.* }}` directly. The script turns off shell glob expansion before splitting `test-globs`. The action writes the job summary and a `::warning` annotation for each weak test, and it refuses to run under `pull_request_target`.
- **Alternatives.** A Docker action, which runs in its own container without the caller's packages, or a JavaScript action, which would only add Node code around a Python tool.
- **Why.** A composite action uses the same Python and packages the caller already set up. Pasting an input into a script lets a crafted value run shell commands. The workflow runs the PR's own code, so `pull_request_target`, which has the base repository's secrets and a token that can write, would let a malicious PR steal them. The example workflow explains this in a comment.

## 18. A --python option

Source: build.

- **Context.** The brief's CLI runs pytest but doesn't say which Python.
- **Choice.** `--python` defaults to the interpreter running the tool.
- **Why.** In the Action and in normal use, the tool is installed next to the project, so the default is right. The option lets someone run the tool from its own virtualenv against a project in another one, which the PYTHONPATH plugin loading (decision 9) supports.

## 19. The tests for the tool

Source: brief, with the structure from the build.

- **Choice.** One end-to-end scenario builds a git repository whose single PR covers every verdict, runs the gate once in a module-scoped fixture, and has a separate small test for each case. Smaller repositories cover the CLI edge cases: merge-base, shallow merge commit, conftest import failure, code loaded from outside the worktree, tests-only PRs, deleted files, custom globs, exit codes, and the report files. Unit tests cover the glob matcher, the fingerprints, the exception rules, and the summary line. The git helper passes `-c commit.gpgsign=false -c core.hooksPath=/dev/null` so a developer's git config can't break the fixtures.
- **Alternatives.** One fixture repository per case.
- **Why.** Each gate run starts several pytest processes, so running the gate once for all the scenario cases keeps the whole suite at about seven seconds, and each case still fails on its own with a clear name.
- **Verified.** 72 tests pass on Python 3.13 with pytest 9.1.1, and on Python 3.10 with pytest 7.4.4 from a regular (not editable) install, which also shows the wheel includes the plugin file. The action's run script was tested locally against a `--depth 2` clone of a merge commit, with the GitHub env variables set by hand.

## 20. What v0 leaves out

Source: build.

- **Flaky tests.** v0 runs each test once. Retrying weak and strong results and labeling disagreements "flaky" is the obvious next step.
- **Fixture-only changes.** A test whose function is unchanged but whose fixture changed is not judged. The research note suggests listing changed fixtures and judging the tests that use them by name.
- **PR comments.** The report goes only to the job summary and annotations. A PR comment needs a write token, which fork PRs don't get.
- **Timeout option.** The 900-second limit per pytest run is fixed in the code.

## 21. Copy git-ignored generated files into the worktrees

Source: changed after the first evaluation on real pull requests.

- **Before.** The worktrees held only tracked files.
- **Evidence.** On plumbum #724, every test file failed to import at head with `No module named 'plumbum.version'`, so nothing was judged. plumbum uses hatch-vcs, which writes `plumbum/version.py` into the checkout during `pip install`, and git ignores that file, so a fresh worktree doesn't have it. setuptools-scm does the same thing, so many projects will hit it.
- **Choice.** After creating the worktrees, the gate lists the git-ignored `.py` files in the checkout with `git ls-files --others --ignored --exclude-standard --directory`. The `--directory` flag reports an ignored folder, such as a virtualenv or `build/`, as one entry, so only loose files come back. The gate copies each file into a worktree when its folder exists there and the file doesn't, and it lists the copied files in the report's warnings.
- **Alternatives.** A `--copy` option for the user to name the files, or rebuilding the version file in each worktree.
- **Why.** The copy needs no setup from the user, and the head test run already imports the same file from the checkout. A test covers it (`test_generated_version_file_is_copied_into_worktrees`).

## 22. Docstring edits don't make a test modified

Source: changed after the first evaluation on real pull requests.

- **Before.** The fingerprint from decision 6 included the docstring, so a test whose docstring was the only change counted as modified and was judged.
- **Evidence.** On instructor #1857, `test_validate_model_json_error` changed only its docstring and a comment, and it came out weak at base, which added a false signal to the report.
- **Choice.** `function_fingerprint` drops the function's docstring before `ast.dump`.
- **Why.** A docstring doesn't change what the test does, just as a comment doesn't. A unit test covers it (`test_fingerprint_ignores_docstring_edits`).

## 23. Only .py files go to pytest

Source: changed after the first evaluation on real pull requests.

- **Before.** Every changed file that matched the patterns, except `conftest.py`, was passed to pytest.
- **Evidence.** On robotframework-robocop #1764, the PR adds `.robot` fixture files under `tests/`. With `--test-glob 'tests/**'` to bring them along, the gate passed `test_types.robot` to pytest, and pytest stopped with `ERROR: not found ... (no match in any of [<Dir source>])` and exit code 4, so no test was judged. pytest reports this for a file whose name starts with `test_` but isn't a Python test module.
- **Choice.** Matched files that don't end in `.py` are still copied to base, but only `.py` files are passed to pytest.
- **Why.** The patterns decide which files travel with the tests, and pytest only collects Python files. A test covers it (`test_non_python_fixtures_are_copied_but_not_collected`).

## 24. Data files under tests/ travel by default, and a file missing only at base is inconclusive

Source: changed after the Codex review (finding 1).

- **Before.** The default patterns were `test_*.py`, `*_test.py`, `tests/**/*.py` and `**/conftest.py`, so a data file the PR added under `tests/` stayed behind, and the base run raised `FileNotFoundError` when the test read it. Decision 10 called any exception other than the listed ones strong, so the test came out strong without ever checking the old code. The evaluation described this as open bug 5.
- **Evidence.** A scratch repository reproduced it. The PR refactors `double()` without changing its behavior and adds `tests/data/expected.json` with a test that reads it. The gate on main called the test STRONG with `FileNotFoundError`. A second scratch case showed a related false strong that no exception rule can catch: the PR changes an existing data file outside the patterns, the base run reads the old version, and the test fails with a `KeyError`. The same thing would happen with an assertion, so the fix has to be about copying inputs, not about exception types.
- **Choice.** The default pattern `tests/**/*.py` became `tests/**`. Decision 23 already copies non-Python matches without passing them to pytest, so the wider pattern is safe. The plugin records the path of a `FileNotFoundError` when it is inside the worktree, and the gate checks whether that path exists in the head commit. If it does, the test is inconclusive, and the reason names the file and suggests a pattern. A `FileNotFoundError` for a file the head commit doesn't have either still counts as strong, because then the old code failed to create or find something the new code handles.
- **Alternatives.** Call every `OSError` inconclusive. That would also hide a real fix, such as code that used to crash on a missing config file and now falls back to defaults.
- **Why.** The wider default removes the common case, and the head-commit check catches the rest without guessing from the exception type alone. Three tests cover it (`test_new_data_file_under_tests_travels_with_the_default_globs`, `test_file_missing_at_base_but_present_at_head_is_inconclusive`, and `test_file_missing_at_both_base_and_head_commits_stays_strong`). On the evaluation it changed no verdicts, since robocop's fixtures already travelled under `--test-glob 'tests/**'` and its default-globs run gave the same verdicts.
