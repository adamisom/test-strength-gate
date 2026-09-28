# Evaluation on merged pull requests

This page gives the per-PR numbers behind the evaluation summary in the README. The gate ran from the command line on 2026-09-25. These runs were from the command line, not as a step in a GitHub workflow. The action's first workflow run was on a demo pull request on 9/28/26 (see the README).

## How the pull requests were chosen and run

The sample is 8 merged pull requests from open-source Python projects, each written by a coding agent. Between them they cover five agents: GitHub Copilot's coding agent, OpenAI Codex, Claude Code, Cursor and Devin. Each PR touches Python source and at least one test file, adds at least one test function, and changes fewer than 500 lines. A ninth PR, robotframework-robocop #1764, ran as an extra case because it adds test fixtures that are not Python files, and it is kept out of the totals.

For each PR, base was the merge commit's first parent and head was the merge commit. Each repository got its own virtualenv with an editable install of the project and its test dependencies. The gate ran with a clean environment, so no API keys were passed to the tests. A second run gave the same verdicts. The evaluation was run again after each round of fixes to the tool, and every run gave the same verdicts for the same tests.

## Results

| Repository | PR | Judged | Strong | Weak | Inconclusive |
| --- | --- | --- | --- | --- | --- |
| tomerfiliba/plumbum | [#724](https://github.com/tomerfiliba/plumbum/pull/724) | 1 | 1 | 0 | 0 |
| SigmaHQ/pySigma | [#553](https://github.com/SigmaHQ/pySigma/pull/553) | 4 | 4 | 0 | 0 |
| python-cachier/cachier | [#279](https://github.com/python-cachier/cachier/pull/279) | 3 | 1 | 2 | 0 |
| Cisco-Talos/EvidenceForge | [#316](https://github.com/Cisco-Talos/EvidenceForge/pull/316) | 1 | 1 | 0 | 0 |
| PrimeIntellect-ai/verifiers | [#836](https://github.com/PrimeIntellect-ai/verifiers/pull/836) | 3 | 0 | 0 | 3 |
| aurelio-labs/semantic-chunkers | [#55](https://github.com/aurelio-labs/semantic-chunkers/pull/55) | 6 | 5 | 1 | 0 |
| huggingface/OpenEnv | [#1171](https://github.com/huggingface/OpenEnv/pull/1171) | 9 | 7 | 2 | 0 |
| 567-labs/instructor | [#1857](https://github.com/567-labs/instructor/pull/1857) | 3 | 1 | 2 | 0 |
| Total, 8 PRs | | 30 | 20 | 7 | 3 |
| MarketSquare/robotframework-robocop (extra) | [#1764](https://github.com/MarketSquare/robotframework-robocop/pull/1764) | 7 | 4 | 3 | 0 |

Of the 30 judged tests, 7 (23%) passed without the source change, and 4 of the 8 PRs had at least one such test. No test was skipped or broken at head.

A weak verdict is a fact about one test, and it says nothing about whether the PR's change is correct. Other tests in the same PR, or tests elsewhere in the project, may cover the same behavior.

## Hand check of the weak tests

Each weak test was read next to the PR's source diff to see why it passes without the change.

### Tests that never reach the changed code (3)

- cachier #279, `test_enable_caching_after_decorator_definition`. The fixed bug only shows up when `set_global_params` runs after a function is decorated. This test calls `set_global_params` before it decorates the function, so it passes with or without the fix.
- cachier #279, `test_disable_caching_after_decorator_definition`. This test uses only `enable_caching()` and `disable_caching()`, which worked the same way before the change. The third test in the PR, `test_original_issue_scenario`, does reproduce the bug and is strong.
- instructor #1857, `test_json_decode_error_caught_by_retry`. The PR describes this test as the regression test for issue #1856. At base, the test's input raises a Pydantic `ValidationError` before it reaches the changed line, and the test accepts either exception type, so it passes at base. The test `test_validate_model_json_error_non_strict` in the same PR calls the changed function directly and is strong.

### Tests that are weak for an expected reason (4)

- instructor #1857, `test_validation_error_caught_by_retry`, checks retry behavior that existed before the PR.
- semantic-chunkers #55, `test_regex_chunker_acall_matches_call`, checks that the async and sync calls agree, and the code that makes them agree is the same at base and head.
- OpenEnv #1171, `test_append_does_not_read_the_entire_results_file`, checks how the new code is written rather than the fixed behavior. The old code never read the file, so the test passes there.
- OpenEnv #1171, `test_append_after_unterminated_blank_line`, covers an edge case of the new code where the old output was also acceptable.

The 3 weak tests in the extra robocop PR are also expected. They check that bad values for a new option are rejected, and base rejects the whole option because it doesn't exist there.

## Strong verdicts

Of the 20 strong verdicts, 15 come from a failed check, which is 11 assertions and 4 `pytest.raises` blocks that did not raise. The other 5 come from another exception at base: a `ValueError` in EvidenceForge and in instructor, two `JSONDecodeError`s in OpenEnv, and an `AttributeError` on a list in OpenEnv. A hand check found all 5 correct. Because a strong verdict from an exception other than an assertion is a heuristic, the report words those 5 as conditional and names where each exception was raised.

Two other ways to label those 5 tests were considered. A separate verdict for them would give 15 strong and 5 in the new verdict. Calling them inconclusive would give 15 strong and 8 inconclusive, which on this sample would be wrong 5 times out of 5. The tool keeps the current label for now.

## Inconclusive verdicts

All 3 inconclusive verdicts are in verifiers #836, and all 3 are correct. Each test calls `EvalDisplay._display_max_concurrent`, a helper the PR adds, so base fails with an `AttributeError` on the class. The PR also changes the display code to call the helper, and none of the three tests checks the display. The gate can't tell a reviewer that, which is a limit of the method, listed in the README.

## Bugs the evaluation found in the tool

The first run found five bugs in the tool, and all five are fixed, each with a test that fails without the fix. For example, plumbum #724 judged nothing at first because a `version.py` file that the build writes at install time was missing from the worktrees, and instructor #1857 showed a fourth weak test that only had a docstring edit. `DECISIONS.md` entries 21 to 24 and 27 describe the fixes. The table shows the results after the first three fixes, and no later fix, including the fixes from code review, changed a verdict in it.
