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

Source: brief, with details from the build. Changed after the Codex re-review, round 2, in commit `75f4da1` (decision 34).

- **Context.** The tool needs the list of test files the PR changed.
- **Choice.** It runs `git diff --name-status --no-renames -z` and keeps the paths that match the patterns. Deleted files used to be ignored. Since `75f4da1`, the gate records them, and it removes the ones that match the patterns from the base run. A pattern without a slash matches the file name in any directory, and a pattern with a slash matches from the repository root, as in `.gitignore`. The glob matcher is a small function that turns `*`, `**/` and `?` into a regular expression.
- **Alternatives.** `fnmatch`, or `PurePath.full_match`.
- **Why.** `fnmatch` lets `*` match `/`, so `tests/*.py` would also match files in subdirectories, and `full_match` needs Python 3.13. With `--no-renames`, a renamed test file is a delete plus an add, so its tests count as new, the old path is removed from the base run when it matches the patterns, and the tool doesn't need extra code for renames. `-z` keeps paths with spaces intact.

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
- **Changed.** The Codex review (finding 6) showed that a change to a test's class, such as a new `@pytest.mark.usefixtures`, was missed. Decision 29 adds the class context to the fingerprint.

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
- **Choice.** The tool passes whole test files to pytest with `--continue-on-collection-errors`. The collect-only runs get the changed `.py` test files other than `conftest.py`, and the judging runs get only the files that hold a judged test. The plugin reads the wanted IDs from a file and deselects every other test in `pytest_collection_modifyitems`. A wanted test that never shows up at base is inconclusive, with the reason "not collected at base".
- **Alternatives.** Pass node IDs and retry file by file when pytest exits with code 4.
- **Why.** One missing test can't hide the results of the others, and only the judged tests run.

## 9. A small recorder plugin instead of JUnit XML

Source: brief, refined in the build.

- **Context.** Classification needs the phase (setup, call, teardown) and the exception type of each failure.
- **Choice.** `tsg_recorder.py` is a pytest plugin loaded with `-p tsg_recorder`. The tool puts the plugin's directory on `PYTHONPATH`. The plugin reads `call.excinfo` in `pytest_runtest_makereport` and writes JSON at the end of the session. The collect-only runs use the same plugin, so the tool reads collected IDs from JSON instead of parsing the `-q` output.
- **Alternatives.** JUnit XML, pytest-json-report, or parsing the terminal output.
- **Why.** The research note showed that JUnit XML writes the same `<failure>` element for an AssertionError and an AttributeError, so it can't support the rules. pytest-json-report hasn't had a release since 2022. The plugin is a plain module on `PYTHONPATH` rather than an import from the installed package, so it works even when the tool runs from a different virtualenv than the project (the `--python` option). The plugin attaches its data to `report.user_properties` so it should reach the main process under pytest-xdist, but I haven't tested xdist.
- **Changed.** Decision 36 tested pytest-xdist and fixed the one check that didn't work under it.

## 10. Classification rules

Source: brief, widened during the build and after research.

- **Context.** A test that fails at base can fail because it checks behavior, or because the API it calls doesn't exist yet. Only the first kind shows the test would catch the change going missing.
- **Choice.** `failure_kind` in `classify.py` holds all the exception rules in one place.
  - AssertionError and pytest's `Failed` are strong.
  - ImportError, ModuleNotFoundError and NameError are inconclusive.
  - A TypeError whose message matches the signature patterns, such as "unexpected keyword argument", is inconclusive. Decision 26 changed its reason when the old project code raised it.
  - AttributeError depends on the object, as decision 11 explains.
  - Any other exception, such as a ValueError or KeyError, is strong, and the reason says it was not an assertion.
  - A FileNotFoundError for a file that exists at head is inconclusive. Decision 24 added this.
  - A setup error, a teardown error, a collection error, and a failed run are all inconclusive.
- **Surprise.** A `pytest.raises` block that doesn't raise fails with `Failed: DID NOT RAISE`, and `Failed` is not a subclass of AssertionError, so the brief's rule would have missed a common kind of strong test. `pytest.fail()` raises the same exception.
- **Alternatives.** Count only AssertionError as strong, and call other exceptions inconclusive.
- **Why.** A test that gets a KeyError from the old code has still seen the old code behave differently, and that is the question the tool asks.
- **Reviewed.** The Codex review (finding 2) asked for this rule to become inconclusive by default. Decision 25 explains why it stays, with measurements.

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
- **Changed.** A test file that fails to import at head used to drop out with only a warning. Decision 30 gives it a row.
- **Changed.** A test that was not collected or not run at head is inconclusive, not broken, since decision 37.

## 13. A conftest.py that fails to import makes the whole run inconclusive

Source: build, confirmed by research.

- **Context.** If the PR's `conftest.py` imports a module that only exists at head, the base run fails before any test is collected.
- **Surprise.** A conftest.py in a directory on the command line is loaded when pytest starts, and an ImportError there makes pytest exit with code 4 before the session starts. So the plugin never writes its JSON and there are no per-test results.
- **Choice.** When the JSON is missing, or pytest exits with code 2, 3 or 4 and nothing was recorded, the runner takes the last `E` line of pytest's output as the reason, and every test in that run is inconclusive with it. A per-run timeout of 900 seconds does the same for a hung run.
- **Why.** The reason tells the reviewer exactly what happened, e.g., "pytest could not start at base: ModuleNotFoundError: No module named 'newhelpers'".
- **Changed.** Without an `E` line, the reason is the last line that says "error:", since decision 41.

## 14. Making sure the base run really tests the base code

Source: build, extended after research.

- **Context.** Projects usually install themselves into the environment, often with `pip install -e .`. An editable install points at the real checkout, which is at head, and a regular install puts a copy of head in site-packages. Either way, the base run could import head code, and every test would look weak with no sign of the problem.
- **Choice.** The runner puts the worktree root and `src/` at the front of `PYTHONPATH`, which comes before site-packages and before the finders that editable installs add. It also sets `PYTHONDONTWRITEBYTECODE=1` so stale `.pyc` files can't survive a source swap. At the end of each run, the plugin lists the project modules that loaded from outside the worktree. A project module is one whose name exists at the worktree root or in `src/`, or any module loaded from the real checkout outside its virtualenv. If the base run has any, all its results become inconclusive, and the reason names the module and the path. For the head run it's only a warning.
- **Alternatives.** Check with `importlib.util.find_spec` before running, as pyrite does, or require users not to install the project.
- **Why.** The path fix covers the two usual layouts, and the check catches the rest instead of reporting false weak results. A test puts the real checkout on `PYTHONPATH` with the package in `lib/`, which is what an editable install does, and checks that the base run is marked inconclusive.
- **Changed.** The Codex review (finding 5) showed that a regular install of a package in `lib/` was not caught. Decision 28 widened the check.

## 15. Tests-only PRs and refactor-like PRs get their own note

Source: changed after research.

- **Before.** The brief exits with code 1 under `--fail-on weak` whenever any test is weak.
- **Evidence.** A PR that only adds tests for existing behavior has no source change to catch, so all its tests pass at base by design. With the brief's rule, `--fail-on weak` would always fail such a PR.
- **Choice.** The tool counts changed Python files that are not tests. With none, the PR is tests-only. The report says weak results are normal, and `--fail-on weak` doesn't fail it. When source changed and every judged test is weak, the report says once that this is expected for a refactor and suspicious for a feature.
- **Alternatives.** A PR label to opt out, as Yosemite-Crew does, or a `control` marker on tests, as pyrite does.
- **Why.** One sentence at the top reads better than a table of identical warnings, and it needs nothing from the PR author.
- **Changed.** Decision 38 counts changed non-Python files outside the patterns as source too.

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
- **Why.** The wider default removes the common case, and the head-commit check catches the rest without guessing from the exception type alone. Three tests cover it (`test_new_data_file_under_tests_travels_with_the_default_globs`, `test_file_missing_at_base_but_present_at_head_is_inconclusive`, and `test_file_missing_at_both_base_and_head_commits_stays_strong`). On the evaluation it changed no verdicts, since robocop's fixtures already travelled under `--test-glob 'tests/**'` and its default-globs run gave the same verdicts. The head-commit check uses `git cat-file -e`, which reads no file contents. The first version decoded the file as text, and Codex's re-review found that a binary fixture crashed the gate with `UnicodeDecodeError`. A test covers it (`test_binary_file_missing_at_base_but_present_at_head_is_inconclusive`). Files the PR deletes under the patterns were left in the base run until decision 34.

## 25. Strong verdicts from other exceptions say where the exception was raised

Source: build, after the Codex review (finding 2). The verdict rule from decision 10 is unchanged. The wording changed again in decision 33.

- **Context.** Codex argued that "any other exception is strong" is too broad, because a test can fail for a reason unrelated to the source change, and asked for such exceptions to be inconclusive by default, or limited to a reviewed list, with the traceback kept for review.
- **Evidence.** A test only comes out strong if it passes at head, so an error that happens in both runs (an unset environment variable, a broken dependency) is BROKEN_AT_HEAD, not strong. A scratch repository confirmed this. A false strong needs something other than the source code to differ between the two runs. Three scratch cases found such differences: a data file the PR adds (fixed by decision 24), a data file the PR changes outside the patterns (the test fails at base with a `KeyError`, and would fail just the same with an assertion), and a test-support module outside the patterns that the PR changes (the tool counts it as source, so the test does depend on the PR's change). None of these is about the exception type. On the evaluation, 5 of the 20 strong verdicts come from exceptions other than assertions, and the hand check found all 5 correct. Applied to the recorded results, the stricter rules would do this:

  | Rule | Strong verdicts lost on the evaluation |
  | --- | --- |
  | Every other exception is inconclusive (Codex's first suggestion) | 5 of 5: EvidenceForge's and instructor's `ValueError`, OpenEnv's two `JSONDecodeError`s and its `AttributeError` on a list |
  | Strong only if the old project code was on the stack | 2: OpenEnv's `JSONDecodeError`s, which the test's own `json.loads` raises on the file the old code wrote |
  | A list of behavioral types (ValueError, LookupError, ArithmeticError, AttributeError on a builtin, other TypeErrors) | 0 here, but it would also make an exception class defined by the project inconclusive, and the evaluation has no such case to measure |
  | Every `OSError` is inconclusive | 0 here, and it adds nothing beyond decision 24 |

- **Choice.** The rule stays. The plugin now records the innermost frame of the traceback and the innermost frame inside the worktree, and the reason for a strong verdict from another exception says where it was raised, e.g., "raised at src/pkg/mod.py:95" or "raised in library code called from tests/test_x.py:267". The JSON report has both locations.
- **Why.** The only systematic cause of a false strong that the review and the scratch cases found is an input that differs between the runs, and decision 24 handles the one the tool can see. Narrowing by exception type or by stack would have turned correct verdicts into inconclusive ones on real pull requests. Showing the location lets a reviewer check each case. Whether to narrow the rule further is left open until there is evidence from more pull requests. A test covers the new reasons (`test_non_assertion_strong_says_where_it_was_raised`).

## 26. A TypeError about arguments raised inside the old code gets an honest reason

Source: build, after the Codex review (finding 3). The verdict is unchanged.

- **Context.** Decision 10 calls a TypeError whose message is about arguments inconclusive, because it usually means the test calls a new signature. Codex pointed out that the old code can itself call a helper with the wrong arguments, and then the test fails because of a real bug that the PR fixes.
- **Evidence.** A scratch repository reproduced it. The old `total()` calls `_sum(items)` without the `start` argument, the PR passes it, and the new test `assert lib.total([1, 2]) == 3` came out inconclusive with the reason "usually means the new API doesn't exist yet". A quick experiment confirmed where Python raises such an error. It is raised in the frame that makes the call, so a test that passes a new keyword gets the error in the test's own line, and the old code's bad call gets it in the old code. But a decorator's `*args, **kwargs` wrapper in the project also raises it inside project code when a test passes a real new keyword. The evaluation had no TypeError at all, so it can't settle the question.
- **Choice.** The gate marks where each exception was raised: in a file that matches the test patterns, in another file of the project, or in a library. A TypeError about arguments raised inside the project's own code is still inconclusive, but the reason says so and names the line, and says the test may call a new API or the old code may call something wrongly. Raised in the test or in a library, it keeps the old reason.
- **Alternatives.** Call the in-project case strong, which would make the decorator case a false strong. Or look up the function named in the message at base and at head and compare their signatures, which is more precise but needs a symbol diff, as pyrite does.
- **Why.** Goal 2 of the design is never to report a false strong for a missing API, so the ambiguous case stays inconclusive, and the reason no longer claims to know the cause. Tests cover it (`test_argument_type_error_inside_the_old_code_says_the_cause_is_unclear`, `test_argument_type_error_depends_on_where_it_was_raised`).

## 27. A bare assertion's reason shows the failing line and the stderr error

Source: build, after the Codex review (finding 4). This was open bug 4 in the evaluation.

- **Context.** pytest rewrites `assert` statements only in test modules and `conftest.py`, so a bare assert in a helper fails with an empty `AssertionError`. The report then said "Fails at base on a check: AssertionError", which explains nothing.
- **Evidence.** On robocop #1764 the four `test_test_types` cases fail at base in the harness line `assert exc_info.value.exit_code == exit_code` in `tests/formatter/__init__.py`, because base doesn't know the new `test_types` option. The cause is only on captured stderr, whose first line is `InvalidParameterError: AlignTestCasesSection: Failed to import.` and whose last line is `skip_documentation`, the end of a list of accepted arguments. So the last stderr line, which the evaluation proposed, would not have helped here. A scratch repository with the same shape reproduced the bare reason.
- **Choice.** When an AssertionError has no message, the plugin records the source line of the innermost frame inside the worktree, and the last line of captured stderr that contains "error", "exception" or "traceback", or else the last line. Each is cut to 200 characters, and nothing else from the captured output is kept. The reason now reads, for robocop, "Fails at base on a check that has no message, at tests/formatter/__init__.py:106: `assert exc_info.value.exit_code == exit_code`. Last error on stderr: InvalidParameterError: AlignTestCasesSection: Failed to import. Verify if."
- **Alternatives.** Keep the whole captured output, which could put secrets a test prints into the job summary. Or make such tests inconclusive, which would be wrong for robocop, where the expected output files would still catch the feature going missing.
- **Why.** The verdict stays strong, and the reviewer can now see when it is strong for a partly wrong reason. pytest already prints the full captured output to the CI log, which the same people can read, so one bounded line adds little exposure. A test covers it (`test_bare_assertion_reason_shows_the_line_and_the_stderr_error`).

## 28. Any top-level package in the worktree counts as project code

Source: changed after the Codex review (finding 5).

- **Before.** Decision 14 counted a module as the project's own when its top-level name was a `.py` file or package at the worktree root or in `src/`, or when it loaded from the real checkout.
- **Evidence.** Codex pointed out that a regular (not editable) install of a package in `lib/myapp/` loads from site-packages, which is neither under the root or `src/` nor under the checkout. A scratch repository reproduced it with a real `uv pip install .` into a virtualenv (the package in `lib/mypkg`, built with hatchling from the local cache). The base run imported the installed head copy, and the gate on main called the test WEAK, a false result with no warning.
- **Choice.** The plugin also walks the worktree and counts every top-level package, i.e., a folder with `__init__.py` whose parent folder has none, such as `lib/mypkg`. It skips hidden folders and folders that don't hold the project's importable code: `tests`, `test`, `testing`, `docs`, `doc`, `examples`, `example`, `benchmarks`, `scripts`, vendored copies (`vendor`, `vendored`, `_vendor`, `third_party`), and build or environment output. With the fix, the scratch install comes out INCONCLUSIVE, and the reason names `mypkg` and its site-packages path.
- **Alternatives.** Read the package folders from `pyproject.toml`, `setup.cfg` or `setup.py`, which has many formats. Or put each package's parent folder, such as `lib/`, on `PYTHONPATH` so the base run tests the base code instead of giving up. That would turn these inconclusive results into real verdicts, but a folder on the path can also hide an installed package of the same name.
- **Why.** The walk needs no knowledge of build tools and covers any layout that uses `__init__.py` files. It can raise a false alarm if a folder outside the skipped ones holds a package named like an installed third-party package, which would make the whole base run inconclusive. On the evaluation it raised none, across nine repositories, including pySigma, where the walk also finds the subpackages of its namespace package `sigma`. A namespace package outside the root and `src/` is still not recognized, and the README says so. A test covers it (`test_installed_copy_of_a_lib_layout_package_makes_base_inconclusive`).

## 29. A test method's fingerprint includes its class's setup

Source: changed after the Codex review (finding 6).

- **Before.** Decision 6 fingerprinted only the test function's own node, so a PR that changed the class around an unchanged method didn't make it modified.
- **Evidence.** A scratch repository reproduced it. The PR adds `@pytest.mark.usefixtures('fast_lib')` to `class TestD` and leaves `test_d` alone, and the gate on main judged no tests at all.
- **Choice.** For each class that encloses the test, the fingerprint adds the class's decorators, bases and keywords, its class-level statements (attributes and `pytestmark`), and its setup and teardown methods (`setup_method`, `setUp` and the rest) and autouse fixtures. It leaves out the class docstring, the other tests, plain helper methods and nested classes.
- **Alternatives.** Fingerprint the whole class, so any edit to any method judges every test in it again. That would make the common PR that adds one test and one helper to a class re-judge all its old tests, which then mostly come out weak, much like the parametrize surprise in decision 6.
- **Why.** The included parts run for every test in the class, so a change to them changes what each test does. Helper methods and non-autouse fixtures affect only the tests that call them, and v0 already doesn't follow those (the fixture-only limit in decision 20). On the evaluation it judged no new tests and changed no verdicts. Tests cover it (`test_fingerprint_includes_the_enclosing_class_context`, `test_new_class_decorator_makes_an_unchanged_method_modified`).

## 30. A test file that fails to import at head gets its own row

Source: changed after the Codex review (finding 7).

- **Before.** When a changed test file failed to collect at head, for example with a syntax error or an import of a missing module, the gate added a warning and judged nothing in it, since there were no test IDs to judge. If it was the PR's only test file, the summary line said "No added or modified tests to judge."
- **Evidence.** A scratch repository reproduced it with a new test file containing `def test_b(:`. The report's first line said "No added or modified tests to judge", and only the warnings section at the bottom mentioned the file.
- **Choice.** Each changed test file that fails to collect at head, or every changed test file when pytest can't start at head (made INCONCLUSIVE by decision 32), gets one row with the file path as its ID, BROKEN_AT_HEAD as the verdict, and pytest's error line as the reason. The summary line counts these files apart from tests, e.g., "No tests could be judged: 1 changed test file fails to import at head, so its tests were not judged." In a GitHub workflow each such file also gets a warning annotation.
- **Alternatives.** A separate verdict such as UNJUDGED, which would add a sixth label for a case that already fits "does not pass at head".
- **Why.** A PR whose new test file doesn't even import should not look like a PR without tests. The row reuses the existing label, and the file path in the ID column tells it apart from a test. Tests cover it (`test_test_file_that_fails_to_import_at_head_gets_its_own_row`, `test_summary_line`).

## 31. Linux and macOS runners only, and a local test of the action's script

Source: build, after the Codex review (finding 8).

- **Context.** Codex noted that `action.yml` uses bash, bash arrays and `set -f`, that the README didn't say which runners work, and that the action had never run as an Action.
- **Evidence.** Part of the claim is wrong. `shell: bash` also runs on GitHub's Windows runners, through the Git Bash that they ship, so the bash syntax alone doesn't rule Windows out. But the tool's CI runs only on Ubuntu and macOS, and nothing has ever run on Windows. The rest is right: the only evidence for the action's script was a one-time manual run.
- **Choice.** The README states that Linux and macOS runners are supported and Windows is not. No Windows port. A new test (`tests/test_action.py`) reads the `Run test-strength-gate` step out of `action.yml` and runs it with bash in a `--depth 2` clone of a merge commit, with the inputs and GitHub's variables set the way a runner sets them. It checks the default base and head, the `json-report` output, the job summary, the weak-test annotation, and that `fail-on: weak` fails the step. `examples/action-integration.yml` is a workflow that would run the action with `uses: ./` on a toy merge commit, with defaults and with inputs set by hand. It has not been run, and it can't cover a real pull request from a fork.
- **Why.** The local test catches breakage in the script on every CI run. A claim that the action works in GitHub needs a real workflow run, which is still to do.

## 32. A pytest run that fails as a whole at head is inconclusive, not broken

Source: changed after the Codex re-review (remaining finding 2).

- **Before.** Decision 30 gave every changed test file a BROKEN_AT_HEAD row when the collect-only run at head had a startup error. The runner uses that error for a pytest timeout and for a run that produced nothing because pytest could not start or hit an internal error. The same was true of the real head run: a timeout there made every judged test BROKEN_AT_HEAD.
- **Evidence.** Codex pointed out that a valid test file whose run exceeds the 900-second limit was reported as broken at head, although its result at head was never established. A regression test reproduces it with a new test file that sleeps at import and a 3-second limit.
- **Choice.** When the whole pytest run at head fails, each changed test file gets an INCONCLUSIVE row with the file path as its ID and the run's error as the reason, and a test whose real head run fails that way is INCONCLUSIVE. A file that fails to import at head, such as one with a syntax error, is still BROKEN_AT_HEAD. The summary line counts the two kinds of file rows apart, and the workflow annotation says which one it is.
- **Alternatives.** A sixth verdict such as RUN_FAILED. Or keep BROKEN_AT_HEAD for a `conftest.py` that fails to load at head, which pytest also reports as a failed start, since that is often a real break in the PR. The runner can't tell it from an environment problem without parsing pytest's output, so it stays inconclusive, and the reason quotes pytest's error.
- **Why.** BROKEN_AT_HEAD says the test does not pass at head, and a timeout or internal error doesn't show that. Tests cover it (`test_head_timeout_is_an_inconclusive_file_row_not_broken`, `test_a_head_run_that_fails_as_a_whole_is_inconclusive_not_broken`, `test_summary_line_counts_files_whose_head_run_failed_apart_from_broken_files`).

## 33. Strong verdicts from other exceptions are worded as conditional, and changed inputs are named

Source: build, after the Codex re-review (remaining finding 3). The verdict rule from decision 10 is still unchanged.

- **Context.** Codex's re-review did not accept decision 25 as a fix. Showing where an exception was raised helps a reviewer, but the STRONG label still sounds like proof, and decision 24's own counterexample (a data file changed outside the patterns, which makes the base run fail with a `KeyError`) shows the rule can be wrong. Codex suggested either a separate verdict or conditional wording plus a prominent list of changed inputs.
- **Choice.** The reason for a strong verdict from an exception other than an assertion now reads, e.g., "Fails at base with KeyError raised at src/app.py:7: 'total'. Not a check, so inspect the cause: this is strong only if the old code caused it." When the PR changes non-Python files outside the test patterns, other than docs (`.md`, `.rst`, `docs/`, `.github/`), and at least one verdict is strong, the report names them right under the summary line and says the base run used their old versions. The JSON report lists them as `other_files`. Decision 34 adds the files the PR deletes. The README's limits say plainly that this rule is a heuristic and name the counterexample.
- **Alternatives.** A separate verdict for these cases, or INCONCLUSIVE by default. Both change verdicts, so they are left to Adam. Their effect on the evaluation is in `docs/evaluation.md`, under Strong verdicts.
- **Why.** It keeps the five correct strong verdicts in the evaluation and stops the label from overstating what was proved. Tests cover it (`test_strong_from_another_exception_is_worded_as_conditional`, `test_changed_data_file_outside_the_patterns_is_named_at_the_top_of_the_report`).

## 34. The base run also reflects the PR's test-side deletions, and the note names deleted inputs

Source: changed after the Codex re-review, round 2.

- **Before.** The gate listed only added and modified files, as test files and as other inputs. It copied the head version of each test file into the base worktree, but a file the PR deleted stayed there. The changed-inputs note from decision 33 left deletions out too.
- **Evidence.** Codex reproduced a false strong. Base has `tests/data/old.txt`, the PR deletes it and adds a test that checks it is gone. The test passes at head, fails at base on `assert not True` because the stale file is still there, and came out STRONG with no note, under the default `tests/**`. A regression test reproduces it with a refactor of `double()` as the only source change.
- **Choice.** The gate records every file the PR deletes (`deleted_files` in the JSON report) and removes the ones that match the test patterns from the base worktree with `git rm`, right before copying in the changed ones. The order matters when the PR replaces a file with a folder of the same name: removing after the copy made git refuse, and the gate crashed. The base run's test side now matches head's, deletions included. That also covers a deleted `conftest.py` or test helper under the patterns. A deleted non-Python file outside the patterns, other than docs, joins `other_files`, and the note marks it "(deleted)" and says the base run still had it. A deleted `.py` file outside the patterns already counted as source.
- **Alternatives.** Leave the deleted file in place and only name it in the note. That keeps a false strong for a case the gate can fix, since the file is test-side by the user's own patterns.
- **Why.** The base run is meant to be base source plus the PR's whole test side. Three tests cover it (`test_fixture_deleted_under_the_patterns_is_deleted_at_base_too`, `test_deleted_file_outside_the_patterns_is_named_at_the_top_of_the_report`, `test_test_side_file_replaced_by_a_folder_of_the_same_name`).

## 35. More wordings of an argument TypeError are inconclusive

Source: Fable audit, 9/28/26 (TSG-1).

- **Before.** Decision 10 called a TypeError inconclusive when its message matched one of the signature patterns, such as "unexpected keyword argument" or "takes 2 positional arguments".
- **Evidence.** The audit found other wordings that Python uses for the same kind of mismatch. When a PR inserts a parameter and the test passes an existing one by keyword, base raises "f() got multiple values for argument 'b'". When a PR removes a positional-only marker, base raises "got some positional-only arguments passed as keyword arguments". Both scratch cases came out STRONG, although the test only called the new signature. C functions use more wordings, e.g. "takes at most 2 arguments (3 given)" and "expected at most 2 arguments, got 3".
- **Choice.** `ARGUMENT_MISMATCH` also matches "multiple values for argument", "positional-only argument", "takes no keyword arguments", "takes exactly", "takes at most", "takes at least", and "expected N arguments" with or without "at most", "at least" or "exactly". The rest of `failure_kind` is unchanged, so a TypeError about arguments raised inside the project's code still gets the "unclear" reason from decision 26.
- **Alternatives.** The audit's stricter option was to call every TypeError raised in the test's own frame inconclusive when the message names the called function. That would also catch wordings no one has listed yet, but it changes the rule for behavioral TypeErrors raised in the test, and the evaluation has no TypeError to measure it on.
- **Why.** Goal 2 of the design is never to report a false strong for a missing API, and these messages are the same missing-API case in other words. A plain behavioral TypeError, such as "unsupported operand type(s)" or "expected str, bytes or os.PathLike object", still counts as strong. The evaluation had no TypeError, so no verdict changed. Each wording is a case in `test_failure_kind`.

## 36. The misrouted-install check runs in each pytest-xdist worker

Source: Fable audit, 9/28/26 (TSG-2).

- **Before.** The plugin checked for project modules loaded from outside the worktree (decisions 14 and 28) in `pytest_sessionfinish` of the main process. Decision 9 expected the plugin to work under pytest-xdist but had not tested it.
- **Evidence.** Under pytest-xdist the workers import the project and run the tests, and the main process only receives their reports, so it never imports the project and the check found nothing. The audit reproduced it with the package in `lib/mypkg`, a head copy installed in site-packages, and `addopts = -n 2`. The gate called the test WEAK with no warning, which is the false result that decision 28 fixed for runs without xdist. Everything else in the plugin worked under xdist, because the per-test records already travel in `report.user_properties`.
- **Choice.** On an xdist worker, the plugin runs the check after each test phase and adds what it has found so far to the record it attaches to the report. It checks only the modules imported since its last check, and it walks the worktree for project names once per process. The main process merges these finds into `misrouted`, and at the end of the session it still runs the full check on its own modules, which is the whole check when xdist is not in use. `pytest-xdist` joined the `test` extra, and two tests run the gate on a project with `-n 2` in `addopts`, one plain and one with an installed copy. They skip when pytest-xdist is not installed.
- **Alternatives.** Run the check once in each worker's `pytest_sessionfinish` and send it to the main process. xdist has no simple channel for that at the end of a session, while the report already reaches the main process. Or add `-p no:xdist` to every run, which would break projects whose `addopts` use `-n`, since the option would then be unknown.
- **Why.** The check has to run where the imports happen. Checking only new module names keeps the cost small for large suites. The evaluation ran without xdist, so no verdict changed.

## 37. A project's -x can't stop the judged runs, and a test without a result is inconclusive

Source: Fable audit, 9/28/26 (TSG-3).

- **Before.** The runner added nothing to override a project's `-x` or `--maxfail`. Any judged test without a result was reported as "not collected", which made it INCONCLUSIVE at base and BROKEN_AT_HEAD at head (decision 12).
- **Evidence.** Many projects put `-x` in `addopts`. The gate runs all the judged tests in one pytest process per worktree, so at base the first strong test stopped the run. In the audit's scratch case, three new tests (one strong, then one weak, then one strong) came out 1 strong and 2 inconclusive, with the reason "Not collected at base", although pytest had collected them. A weak test hidden this way is the main thing the tool exists to show. At head, one broken test made every later test BROKEN_AT_HEAD with "not run: not collected", and a test whose parametrize ID changes between runs was also BROKEN_AT_HEAD although it passes.
- **Choice.** The runner appends `--maxfail=0` after the project's arguments, which overrides `-x` from `addopts` or `--pytest-args` (checked on pytest 9.1.1 and 7.4.4). The plugin's `items` list, which already held the collected tests, is now filled under pytest-xdist too, from the workers' collection. A test in `items` with no result was collected but not run, and it gets its own reason, "Collected at base but not run, e.g. because the session stopped early". At head, a test that was not collected or not run is INCONCLUSIVE, because its result there is unknown, which is the principle of decision 32. A test that runs at head and fails is still BROKEN_AT_HEAD.
- **Alternatives.** Run each test file in its own pytest process, which is slower and still leaves a stop inside one file. Or `-o addopts=`, which would drop every other option the project needs.
- **Why.** A project's wish to stop at the first failure is for its own test runs, and the gate needs a result for every judged test. "Does not pass at head" should mean the test ran at head and failed. Something else can still end a session early, such as `pytest.exit()` or `--sw` in `addopts`, and those tests now say they were not run. On the evaluation every judged test ran at base and at head, so no verdict changed. Tests cover it (`test_x_in_addopts_does_not_stop_the_judged_runs`, `test_collected_but_not_run_at_base_has_its_own_reason`, `test_head_failure_overrides_a_strong_base`, `test_plugin_lists_tests_that_were_collected_but_not_run`, `test_plugin_lists_collected_tests_under_xdist`).

## 38. A PR is tests only when it changes nothing outside its tests and docs

Source: Fable audit, 9/28/26 (TSG-4).

- **Before.** Decision 15 called a PR tests only when it changed no Python file outside the test patterns. Such a PR gets the note that weak results are normal, and `--fail-on weak` doesn't fail it.
- **Evidence.** Python projects also keep behavior in files that are not `.py`, such as a template, a SQL file, a YAML config the code reads, or a C extension. In the audit's scratch case, the PR changes only `greeting.txt`, which `app.py` reads, and adds a test. The report printed the tests-only note, then the changed-inputs note for `greeting.txt`, then a STRONG row, and `--fail-on weak` would have ignored a weak test.
- **Choice.** A PR is tests only when it changes no Python source and `other_files` is empty, i.e. no non-Python file outside the patterns other than docs changed or was deleted. The note now says "changes no source outside its test files and docs". The README's limits also say that a PR whose source change is in a compiled extension gives weak verdicts that mean nothing, because extensions are not rebuilt at base.
- **Alternatives.** Keep the rule and only hide the note when a verdict is strong. That fixes the contradiction in the report, but `--fail-on weak` would still ignore weak tests in such a PR.
- **Why.** Any changed input outside the tests can be the change a test should catch, which is why decision 33 already names these files in the report. A PR that also changes a config file the tests don't read loses the tests-only note, which is the safer mistake. Every evaluation PR changed Python source, and none had other files, so no label or verdict changed. Tests cover it (`test_a_pr_whose_source_change_is_a_non_python_file_is_not_tests_only`, `test_a_pr_that_changes_only_tests_and_docs_is_still_tests_only`).

## 39. A test file that isn't UTF-8 no longer crashes the gate

Source: Fable audit, 9/28/26 (TSG-6).

- **Before.** `gitutil.show` decoded `git show` output as strict UTF-8, and the CLI caught only `GitError`.
- **Evidence.** A Python test file with a `# -*- coding: latin-1 -*-` line and a non-ASCII byte is legal Python. In the audit's scratch case, `git show` of it raised `UnicodeDecodeError` inside `run_gate`, and the step failed with a traceback and exit code 1 instead of a report.
- **Choice.** `show` decodes with `errors="replace"`, so a byte that isn't UTF-8 becomes a replacement character. `cli.main` catches `UnicodeDecodeError` like `GitError` and exits with code 2 and a one-line message, which covers the remaining case of a file path in `git diff` output that isn't UTF-8.
- **Alternatives.** Decode each file with `tokenize.detect_encoding`, which honors the coding line. That is more exact, but the gate only needs a string it can parse to fingerprint test functions, and the base and head versions are decoded the same way, so comparing their fingerprints still works.
- **Why.** A rare encoding should cost at most a message, never a crash. The evaluation had no such file, so no verdict changed. Tests cover it (`test_a_test_file_that_is_not_utf8_is_judged`, `test_a_decoding_error_exits_2_with_a_message`).

## 40. The action installs itself with uv when the chosen Python has no pip

Source: Fable audit, 9/28/26 (TSG-8).

- **Before.** The action's install step ran `"$PYTHON_BIN" -m pip install "$ACTION_PATH"`.
- **Evidence.** A virtualenv made with `uv venv` or `uv sync` has no pip. A project that sets up its environment that way and passes `python: .venv/bin/python` got "No module named pip", and the step failed before the gate ran. The audit confirmed that such an interpreter has no pip.
- **Choice.** The step uses `"$PYTHON_BIN" -m pip` when pip is available, else `uv pip install --python "$PYTHON_BIN"` when `uv` is on PATH, else it fails with an `::error::` that says to install pip or uv in an earlier step. The inputs still reach the script only through env variables.
- **Alternatives.** Skip the install and run the tool from `$ACTION_PATH/src` with `PYTHONPATH`. That needs no pip or uv, but `runner.py` copies `PYTHONPATH` into the pytest runs, so the project's tests would also see a `test_strength_gate` package unless the runner strips it.
- **Why.** A project that uses uv has uv on PATH, so the fallback covers the case the audit found without changing how the tool runs. `tests/test_action.py` now runs the install step too, with a Python that has no pip, once with uv on PATH and once without it. The pip branch is the old command and has no local test, because a pip install of the action builds it with hatchling, which needs the network.

## 41. The cache plugin stays on, with its cache in the run's temp folder

Source: Fable audit, 9/28/26 (TSG-10).

- **Before.** The runner passed `-p no:cacheprovider` so that pytest wrote no `.pytest_cache` into the worktrees. When a run failed as a whole and pytest printed no `E ` line, the reason was the last line of pytest's output (decision 13).
- **Evidence.** Options such as `--ff`, `--lf`, `--nf`, `--cache-clear` and `--sw` belong to the cache plugin. In the audit's scratch case, a project with `--ff` in `addopts` got a usage error (exit code 4) in every run, so every test file was inconclusive. The inconclusive verdict was safe, but the reason was wrong, because pytest prints its usage error and then `inifile:` and `rootdir:` lines, and the reason was the `rootdir:` line.
- **Choice.** The runner keeps the cache plugin and passes `-o cache_dir=<the run's temp folder>/cache` after the project's arguments, so nothing is written into the worktree and cache options in `addopts` work. The cache starts empty in every run, so `--lf` and `--ff` change nothing about which tests run. When pytest's output has no `E ` line, the reason is the last line that contains "error:", and only then the last line.
- **Alternatives.** Keep the plugin off and strip cache options from `addopts`, which means parsing the project's configuration.
- **Why.** The gate should run a project the way its own configuration expects, and a fresh cache in a temp folder has no effect on the verdicts. One side effect is that `--sw` in `addopts` now works, and it stops the run at the first failure as `-x` does, but `--maxfail=0` doesn't override it. The tests after that failure are then inconclusive with the reason "Collected at base but not run" (decision 37), where they used to be inconclusive with a usage error. The evaluation ran with `-p no:cacheprovider` and no cache options, so no verdict changed. Tests cover it (`test_cache_options_in_addopts_still_work`, `test_runs_write_no_cache_into_the_worktree`, `test_a_usage_error_gives_pytests_error_line_not_the_rootdir_line`).

## 42. Annotation paths start at the workspace root and are escaped

Source: Fable audit, 9/28/26 (TSG-11).

- **Before.** The CLI printed `::warning file=tests/test_x.py,...` with the path relative to the repository the gate ran in, and it did not escape the property value.
- **Evidence.** GitHub reads an annotation's file path from the workspace root. When the action's `path` input points at a subfolder, the path was missing that folder, so the annotation attached to nothing. GitHub's rules for workflow commands also say that `%`, `:` and `,` in a property value, and `%`, carriage returns and newlines anywhere, must be escaped. Test IDs can't inject a workflow command, because pytest escapes non-printable characters in IDs, so the exposure was a misplaced or dropped annotation.
- **Choice.** A new option, `--annotation-path-prefix`, gives the repository's path from the workspace root, and the CLI puts it in front of each annotation's file path unless it is `.`. `action.yml` passes the `path` input to it through an env variable, like every other input. The `file=` and `title=` values escape `%`, `:`, `,`, `\r` and `\n`, and the message escapes `%`, `\r` and `\n`.
- **Alternatives.** Have the CLI work out the prefix from `GITHUB_WORKSPACE` and the repository path. That would work only in a workflow and would resolve symlinks in ways that are hard to test, while the action already knows the `path` input.
- **Why.** An annotation is useful only when it lands on the file. The evaluation ran outside GitHub, so no verdict changed. Tests cover it (`test_annotations_are_relative_to_the_workspace_and_escaped`, and `test_action_run_step_on_a_shallow_merge_commit` with the `path` input set to `.` and to a subfolder).

## 43. The action takes both base-sha and head-sha, or neither

Source: Fable audit, 9/28/26 (TSG-12).

- **Before.** The action's run step used its default, the merge commit and its first parent, whenever either input was empty, and filled in only the empty one.
- **Evidence.** A caller who passed only `head-sha` got the "HEAD is not a merge commit" error on a checkout that isn't a merge commit. On a merge checkout, the caller got `HEAD^1` as base against their own head, which mixes the two meanings. The README said both inputs can be passed, and the input descriptions didn't say they go together.
- **Choice.** The run step fails with an `::error::` when exactly one of the two inputs is set. Both input descriptions and the README say to set both or neither. With neither, the default is unchanged.
- **Alternatives.** Fill in the missing one from the merge commit, as before, and document it. Neither half of that mix is what a caller who sets one input by hand is likely to want.
- **Why.** A clear error on the first run is better than a gate that silently judges the wrong commits. The evaluation ran the CLI, so no verdict changed. A test covers both cases (`test_run_step_requires_both_shas_or_neither`).

## 44. The hand check of the OpenEnv strong verdicts is written down, and a test name is fixed

Source: Fable audit, 9/28/26 (TSG-13).

- **Context.** The README says a hand check found all 5 strong verdicts from exceptions other than assertions correct. `docs/evaluation.md` gave the reasoning for EvidenceForge, instructor and OpenEnv's `AttributeError`, but the two OpenEnv `JSONDecodeError`s were explained only in a private triage note. The test `test_deleted_test_files_are_ignored` kept its name from before decision 34, although deleted test files are no longer ignored.
- **Choice.** `docs/evaluation.md` has one sentence for each `JSONDecodeError` verdict that says why it is correct. The old code appends the new record on the same line as a final record that has no trailing newline, so the test's own `json.loads` raises "Extra data" where the first record ends. The positions in the saved messages, characters 21 and 10,036, match the lengths of the first records in the PR's test code. The test is now `test_a_deleted_test_file_has_no_tests_to_judge`, which is what it checks.
- **Why.** A claim in the README should rest on reasoning that a reader can find. No code or verdict changed.

## 45. A source module named like a test file stays test side (a documented limit)

Source: Fable audit, 9/28/26 (TSG-5). Not changed.

- **Context.** The default patterns `test_*.py` and `*_test.py` have no slash, so they match a file name in any folder, including inside the package. The audit's scratch case has `pkg/core.py` import `make` from `pkg/test_utils.py`. The PR changes `make` and adds a test of the new result. Since `pkg/test_utils.py` matches a pattern, its head version is copied into the base run, the test passes there and comes out WEAK with no warning, and the PR is labeled tests only.
- **Choice.** The patterns are unchanged. The README's limits say that such a module is treated as test side, and that projects with such modules should pass a narrower `--test-glob`.
- **Alternatives.** The audit suggested skipping a slash-free match when a parent folder of the path has an `__init__.py` and the path is not under a folder named `tests` or `test`.
- **Why.** Some projects keep real test files inside their package outside a `tests` folder, e.g. `pkg/test_core.py` next to `pkg/core.py`, or `pkg/unittests/test_core.py`. The suggested rule would stop treating those as tests, so the gate would miss real in-package test files and judge nothing in them, without saying so. A source module named `test_*.py` is uncommon, and a project that has one can set the patterns itself.

## 46. Missing APIs that surface as other exceptions stay strong (a documented limit)

Source: Fable audit, 9/28/26 (TSG-7). Not changed.

- **Context.** Decision 10 counts any exception other than the listed missing-API kinds as strong. The audit found four common forms of a missing API that surface as something else, and each came out STRONG in a scratch repository: argparse raising `SystemExit(2)` for a new command-line option, a pydantic-style model raising "Extra inputs are not permitted" for a new field, a registry dictionary raising `KeyError` for a new key, and a test that checks `assert hasattr(mod, "new_func")`. The first three get the conditional wording from decision 33, and the `hasattr` one gets the firm wording "Fails at base on a check".
- **Choice.** The rule is unchanged. The README's limits list the four forms.
- **Alternatives.** The audit suggested message rules in `failure_kind` for wordings that name a rejected option or field, e.g. "unrecognized arguments" and "Extra inputs are not permitted", treating a `SystemExit` from a library with a usage message like an argument TypeError, and the conditional wording for an assertion that contains `hasattr(` or `in dir(`.
- **Why.** Each suggestion changes the verdict rule for exceptions other than assertions, which is an open call for Adam (design doc, call 5: keep the label, add a separate verdict, or make these inconclusive). Adding exception-specific rules before that call would narrow the rule piece by piece without a decision on the whole. The evaluation had none of the four forms, so it can't measure them. The TSG-1 fix (decision 35) was different, because it only widened a rule that already calls argument errors inconclusive.

## 47. A test whose only change is module-level code in its file is not judged (a documented limit)

Source: Fable audit, 9/28/26 (TSG-9). Not changed.

- **Context.** The fingerprint from decisions 6 and 29 covers the test function and the context of its enclosing classes. A change to a module-level constant, a module-level helper the test calls, or a module-level `pytestmark` leaves the function's fingerprint unchanged, so the test is not judged. In the audit's scratch case, the PR changes `EXPECTED = 1` to `EXPECTED = 1.5` in the test file and changes the source to match, and the report says "No added or modified tests to judge". The README's limits mentioned only fixtures.
- **Choice.** The fingerprint is unchanged. The README's limit on fixtures now also names module-level code in the test file, such as a constant, a helper function or `pytestmark`. The report's legend explains the verdicts and doesn't list what isn't judged, so it is unchanged.
- **Alternatives.** Add the file's module-level statements, other than imports, functions and classes, to every fingerprint in that file, the way decision 29 adds a class's context.
- **Why.** A module-level edit would then make every test in the file count as modified, and most of them would come out weak, much like the parametrize surprise in decision 6 and the whole-class option that decision 29 rejected. Following which tests use a changed constant or helper needs the same name tracking as the fixture gap in decision 20, and the two should be solved together.

## 48. The argument TypeError pattern matches only messages Python writes itself
Source: Codex review, 9/28/26 (TSG-16). Changes entry 35.

- **Context.** Entry 35 widened `ARGUMENT_MISMATCH` with more of Python's wordings, but matched them anywhere in the message. Codex showed that old project code raising its own `TypeError("renderer takes at most 2 arguments for this input")` then came out INCONCLUSIVE, although it is a behavioral failure that the PR fixed.
- **Choice.** The pattern is anchored to the shape of the messages Python writes for a call that doesn't fit a signature: the callable's name, then `()`, then one of the known phrases, or, for some C functions, the name followed by "expected N arguments, got M". Every wording the unit tests covered still matches, and qualified names such as `Foo.__init__()` and `<lambda>()` match too.
- **Alternatives.** Decide from the traceback instead of the message, e.g. treat a `TypeError` raised in the test's own frame as a call mismatch. That misses a mismatch raised inside a project decorator's wrapper, which is why entry 26 keeps the message rule.
- **Why.** A message that only sounds like a signature error is the project's own words, so it now falls to the rule for other exceptions: STRONG, with the conditional reason. No saved evaluation report has a `TypeError`, so no evaluation verdict changes.

## 49. A weak verdict says when the base run imported a copied generated file
Source: Codex review, 9/28/26 (TSG-15). Adds to entry 21.

- **Context.** Entry 21 copies git-ignored `.py` files, such as the `version.py` that hatch-vcs or setuptools-scm writes at install time, from the checkout into both worktrees, because without them plumbum #724 judged nothing. The checkout's copy was generated for head. Codex showed a PR that bumps the version and adds a test of it: the base run imports head's `version.py`, the test passes there, and it comes out weak with nothing but a line under Warnings.
- **Choice.** The gate passes the files it copied into the base worktree to the plugin, which reports the ones some imported module was loaded from, in every pytest-xdist worker too. When the base run imported one, each weak verdict's reason names it and says it was generated for head, and the report adds a note at the top. The JSON has them as `copied_imported`. No verdict changes.
- **Alternatives.** Make such a weak verdict inconclusive. In the saved evaluation reports only plumbum #724 copied a file, and its one verdict is strong, so that would change no published number, but it is a change to the verdict rules and waits for Adam. Rebuilding the project at base would be correct, but it runs the project's build, which the tool never does.
- **Why.** The wrong weak was silent, and now it isn't. A weak verdict is already a prompt for a reviewer, and the caveat tells them what to check.

## 50. A pass at base that imported a copied generated file is inconclusive
Source: Codex review, 9/28/26 (TSG-15), Adam's call on 9/29/26. Changes entry 49.

- **Context.** Entry 49 kept such a test weak and added a caveat to its reason, and left the stricter option for Adam. The copied file was generated for head, so a pass at base can come from head's values in it, and then it says nothing about the base code.
- **Choice.** When the base run imported a git-ignored `.py` file the gate copied from the checkout, a test that would be weak is inconclusive, and its reason names the file and says it was generated for head. The report's note at the top now appears when such a run has any test that passed at base, and says those tests are inconclusive rather than weak. The legend and the README's verdict table and limits say so too.
- **Alternatives.** Keep the weak verdict with the caveat, as entry 49 did. Rebuild the project at base, which runs the project's build, and the tool never does that.
- **Why.** A weak verdict asks a reviewer to act on the claim that the test passes without the source change, and here the gate can't back that claim. In the saved evaluation reports only plumbum #724 copied a file, and its one verdict is strong, so no published number changes.

## 51. Django's, SQLAlchemy's and CPython's C wordings for a call that doesn't fit count as a new API
Source: Fable audit 2, 9/29/26 (TSG-17 and TSG-21). Changes entry 48.

- **Context.** Entry 48 anchored `ARGUMENT_MISMATCH` to the shapes Python writes itself. Libraries that build objects from keyword arguments write their own, equally fixed shapes. Django writes "Author() got unexpected keyword arguments: 'nickname'", which the substring pattern before entry 48 matched, so a test of a model field the PR adds became STRONG again. SQLAlchemy writes "'nickname' is an invalid keyword argument for Author", which never matched. CPython's Argument Clinic functions write "'strict' is an invalid keyword argument for int()" (3.10 to 3.12), "open() missing required argument 'file' (pos 1)" and, from 3.13, "this function got an unexpected keyword argument 'x'".
- **Choice.** The pattern adds these five shapes, each anchored the way entry 48's are: the callable's name and `()` first, or a quoted identifier followed by "is an invalid keyword argument for" and a name, or the exact "this function got an unexpected keyword argument". An end-to-end test uses a hand-written class that raises Django's wording, so the suite needs no Django.
- **Alternatives.** Go back to matching phrases anywhere, which reopens entry 48's false INCONCLUSIVE for a project's own message.
- **Why.** A test that constructs a model with a field the PR adds is the plainest missing-API case, and goal 2 says it must not be strong. No saved evaluation report has a `TypeError`, so no evaluation verdict changes.

## 52. A test without a call record did not pass
Source: Fable audit 2, 9/29/26 (TSG-22). Adds to entry 38.

- **Context.** `summarize` returned passed when no recorded phase failed or was skipped. A test body that calls `pytest.exit()` ends the session without a call report, so it had only a passed setup record and came out WEAK, although its body never finished.
- **Choice.** Without a call record, the outcome is "collected but not run", so the verdict is INCONCLUSIVE with that reason, at base or at head.
- **Alternatives.** None worth keeping. Every test that really passes has a call record.
- **Why.** A pass the gate didn't see can't be a weak verdict. Every saved evaluation outcome that passed came from a call record, so no verdict changes.

## 53. The "collected but not run" reasons are tested end to end
Source: Fable audit 2, 9/29/26 (TSG-23). Adds to entry 38.

- **Context.** The plugin's `items` list and the classifier's `collected` flag were each tested alone. With `--maxfail=0`, no end-to-end test reached either "Collected ... but not run" reason, so replacing `test_id in base_items` or `test_id in head_items` with `False` left the suite green.
- **Choice.** Two end-to-end tests: `--sw` in `addopts` at base, and `pytest.exit()` at base and at head. Both mutations now fail them.
- **Why.** The verdict was right either way, but the reason is what tells the reviewer why.

## 54. pytest's output is decoded with replacement
Source: Fable audit 2, 9/29/26 (TSG-27). Adds to entry 39.

- **Context.** The runner decoded pytest's stdout and stderr strictly. pytest replaces bad bytes in captured test output, but a conftest or plugin that writes to a file descriptor directly bypasses that. One such byte raised `UnicodeDecodeError`, and the CLI reported it as git output it couldn't read, with no report.
- **Choice.** The runner decodes pytest's output as UTF-8 with replacement. The CLI's message for a decoding error no longer names git as the source.
- **Why.** The gate reads its results from the plugin's JSON, and uses pytest's text only for the reason when a run fails as a whole, so a replaced byte costs nothing.

## 55. A copied file read without an import still counts
Source: Fable audit 2, 9/29/26 (TSG-24). Adds to entries 49 and 50.

- **Context.** Entries 49 and 50 find a copied generated file through the modules loaded from it. A package that reads its version file with `exec(open(...).read())` in `__init__.py` loads no module from it, so the base run used head's value and the test came out weak with no caveat.
- **Choice.** A copied file also counts when the base run imported a module in the same folder whose source names the file, e.g. `_version` in `pkg/__init__.py`. Such a test is then inconclusive, as in entry 50, and the reason and the note say the base run "imported or read" the file.
- **Alternatives.** Count the file whenever its package was imported, as the audit suggested first. The existing test with a package that never touches its copied file would then turn inconclusive, and so would every weak verdict in a project whose package holds any generated file. Or add the caveat whenever any file was copied, which is never silent but noisier still.
- **Why.** The name check covers `exec`, `open` and `runpy` in the package itself without flagging packages that don't use the file. A module elsewhere that builds the path from pieces is still missed; the warning that lists copied files stays as the fallback.

## 56. The copied-file check looks at each module once
Source: Fable audit 2, 9/29/26 (TSG-26). Adds to entry 49.

- **Context.** Under pytest-xdist the check runs after every test phase, and it resolved the path of every module in `sys.modules` each time. With 300 tests and a copied file, the gate took three times as long.
- **Choice.** It keeps the modules it has seen, as the per-worker misrouting check does (entry 40), resolves each copied path once, and looks only at new modules.
- **Why.** It can't change a verdict, and the fix is small.
