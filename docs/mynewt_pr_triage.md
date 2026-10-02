# Apache Mynewt PR triage

Instructions for the model that decides which merged Apache Mynewt PRs can
become EmbedEval benchmark instances.

This runs after the Mynewt hard filters in
`scripts/filter_candidates.py`. Every PR you see is already merged, changes a
path under a `selftest` package, and changes production source outside that
package. Do not re-check those mechanical requirements. You must still verify
that it has an originating issue and that the package is native-compatible.

---

## What you are deciding

An instance gives a coding agent `apache/mynewt-core` at the commit before the
PR, plus only the PR's selftest changes. The agent must implement the missing
production behavior without seeing the upstream fix.

Accept only when both statements are true:

1. The PR makes a behavioral production change.
2. A changed selftest deterministically distinguishes the unfixed code from
   the fixed code.

A fix without a detecting test cannot be graded. Test maintenance without a
production behavior to implement is not a coding task.

---

## What you are given

One enriched JSON record produced by:

```bash
python scripts/filter_candidates.py enrich --repo mynewt --pr <PR>
```

It contains:

- PR title, body, labels, commits, and complete per-file diff
- Linked issue bodies and comments
- PR discussion, reviews, and inline review comments
- The complete contents of every changed selftest package, including
  its `pkg.yml`, suite registration, and testcase sources
- `base_commit` and `head_commit`, the exact PR branch range used to separate
  test changes from production changes

Read the linked issue, production diff, test diff, and complete selftest
package. Filenames alone do not establish that a test detects the change.

---

## What we can run

Mynewt support is deliberately native-only:

| platform | execution path |
|---|---|
| `native` | `hw/bsp/native` with `compiler/sim`, compiled to and executed as a Linux `amd64` host ELF |

There is no supported QEMU target in this workflow. Reject a PR whose changed
test requires a physical board, MCU-specific peripheral, radio, sensor,
external device, or another simulator. A package being named `selftest` is not
enough if its actual dependencies require hardware.

The generated runner executes exactly one package using:

```bash
newt test @apache-mynewt-core/<test_path>
```

It then executes the produced ELF directly and reads the Mynewt testutil
`[pass] suite/test` and `[FAIL] suite/test` lines. Missing, unexpected, or
crashed testcases invalidate the instance.

---

## Accept when all of these hold

1. **A changed testcase detects the production change.** Name the assertion,
   represented by a named `[FAIL] suite/test` result without the fix and a
   named `[pass] suite/test` result with it. Build-only failures, crashes, and
   missing testcase results are invalid because the runner classifies them as
   infrastructure errors rather than regression failures.
2. **The PR has an originating issue.** `linked_issues` must contain the issue
   that reports or requests the behavior. A PR, review comment, or unrelated
   issue is not a substitute.
3. **The test package is host-native.** Its `pkg.yml` is a unit-test package
   and its dependency chain does not require real hardware.
4. **The test and fix can be separated.** Applying only the changed selftest
   files to the base revision must leave the production bug present.
   If a test needs support outside its selftest directory, inspect the exact
   `base_commit..test_commit` and `test_commit..head_commit` diffs using git or
   the GitHub commit views. Confirm that every earlier support hunk only makes
   the test executable and does not implement any part of the production
   behavior. If you cannot inspect both commit ranges, reject the PR.
5. **The expected testcase inventory is exact.** List every testcase the
   generated runner should observe, divided into the fields below.
6. **The behavior is deterministic.** Do not accept timing-sensitive or flaky
   regressions whose result changes across runs.

Features are eligible when their tests directly specify the missing behavior;
the change does not have to be labeled a bug fix.

---

## Reject when any of these hold

- The changed test needs physical hardware or an MCU-specific BSP.
- No originating GitHub issue is present in `linked_issues`.
- The PR changes production code and tests, but the test does not assert the
  changed behavior.
- The test only checks an example application, build configuration, or sample
  rather than a regression behavior.
- The change is a refactor, cleanup, format change, comment change, dependency
  update, or warning-only maintenance with no behavioral oracle.
- The test merely changes because the old test was flaky, overly strict, or
  wrong.
- The only plausible execution path is QEMU or another unsupported simulator.
- The only detecting oracle is a build failure, crash, missing testcase, or
  incomplete result inventory rather than a clean named testcase failure.
- Test and production changes overlap in a way that cannot produce a clean
  test-only patch.

---

## Output

Return exactly one JSON object, with no prose outside it:

```json
{
  "pr": 3680,
  "verdict": "accept",
  "confidence": "high",
  "change_type": "fix",
  "platform": "native",
  "problem_statement": "JSON string values that do not fit in the destination including the terminating NUL must return JSON_ERR_STRLONG. Attribute names exactly JSON_ATTR_MAX characters long must be accepted for lookup and return JSON_ERR_BADATTR when unknown; longer names must return JSON_ERR_ATTRLEN.",
  "fail_to_pass": [
    "test_json_suite/test_json_decode_errors"
  ],
  "pass_to_pass": [
    "test_json_suite/test_json_simple_decode",
    "test_json_suite/test_json_simple_encode"
  ],
  "baseline_tests": [
    "test_json_suite/test_json_simple_decode",
    "test_json_suite/test_json_simple_encode"
  ],
  "compatibility_cflags": [],
  "detecting_test": "test_json_decode_errors checks the string-capacity and attribute-name boundaries and reports failed assertions on the unfixed decoder.",
  "reason": "The changed native selftest directly exercises both boundary behaviors and fails deterministically without the production fix."
}
```

Field rules:

- `verdict`: `accept` or `reject`. A rejection may omit every later field
  except `confidence` and `reason`.
- `confidence`: `high`, `medium`, or `low`.
- `change_type`: `fix` or `feature`.
- `platform`: exactly `native`. Never output a QEMU board.
- `problem_statement`: describe the observed problem and expected behavior.
  Do not reveal the upstream patch, implementation, function to edit, or exact
  source location.
- `fail_to_pass`: full `suite/testcase` identifiers for changed tests that fail
  before the fix and pass after it.
- `pass_to_pass`: every other testcase expected when the patched selftest
  package runs. This must include unchanged passing cases and any newly added
  passing cases.
- `baseline_tests`: testcase identifiers present and passing before the
  selftest patch is applied. This is usually `pass_to_pass` minus newly added
  cases. It may be empty for a new package.
- `test_commit`: optional SHA of the final test-only commit when tests and
  their support changes land before the production fix. Omit this when the PR
  does not need a commit boundary between test setup and production code. You
  must inspect the exact per-commit diffs before selecting this boundary; the
  enriched record's aggregate file diff is not enough by itself.
- `test_support_paths`: optional paths outside the selftest package that are
  changed by `test_commit` solely to make the regression tests executable.
  This requires `test_commit`; never include the actual production fix here.
- `compatibility_cflags`: infrastructure-only compiler flags needed to build
  an older base revision with the current host compiler. Usually empty. Never
  use this to suppress a failure caused by the PR's missing production change.
- `gold_patch_three_way`: optional boolean; use `true` only when the upstream
  production diff overlaps context changed by the test-only patch and needs
  `git apply --3way` during known-answer validation.
- `detecting_test`: name one concrete test and explain how it detects the
  missing behavior.
- `reason`: one or two sentences justifying the verdict.

When uncertain about hardware compatibility or whether an assertion is
causal, use `"verdict": "reject"`. Unlike Zephyr's broad simulator set,
Mynewt has exactly one supported execution path here, so native runnability is
a hard requirement.
