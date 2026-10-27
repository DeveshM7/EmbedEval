# Apache Mynewt PR triage: classify the test, find the fix

You review one merged `apache/mynewt-core` PR that changes a unit-test
package (a package whose `pkg.yml` says `pkg.type: unittest`; today usually
named `selftest`, before 2019 `test` or `test-*`). EmbedEval turns a
production change plus a unit-test change into a task: an agent gets
`mynewt-core` from **before** the production change, plus the test changes, and
must make the tests pass without seeing the upstream fix. You decide what kind
of change this PR is and find the production change its test detects. That
change is usually in the same PR, but a PR that only adds or changes tests can
detect a fix merged earlier: then you find that fix yourself. Nobody has
paired it for you.

PRs you accept at `high` or `medium` confidence are generated and run: the
old code plus the new test must fail, the fixed code plus the same test must
pass. That run, not you, decides validity, so accept every PR that plausibly
fits, with the evidence. Accepts at `low` are only recorded.

There is no linked-issue requirement. An issue (GitHub or a `MYNEWT-nnn` id)
is only evidence, and a good problem statement can come from the PR text and
diff alone.

## 1. Classify the PR (`kind`)

- `fix_with_test`: the PR fixes a bug in production code (outside every
  unit-test package) and changes or adds a test that fails on the code
  before the PR and passes after it.
- `feature_with_test`: the PR deliberately adds or changes behavior or an
  interface, and a changed test specifies it. With the old code the test
  fails an assertion or does not compile because the interface does not exist
  yet. Not labeled a bug fix is fine.
- `test_only_detects_earlier_fix`: the PR changes no production behavior the
  test detects (it only adds or rewrites tests, or test support), yet a
  changed test fails on the code as it was before some earlier merged fix and
  passes on the code with it. You must find that earlier PR or commit.
- `maintenance`: reject. No changed test distinguishes old from new
  behavior: refactor, formatting, comment, dependency, build or `pkg.yml` or
  `syscfg` plumbing, a test edited because it was flaky, too strict or wrong,
  a test of long-standing behavior with no change that it detects, a revert,
  an import or rename of a package, a test that is only initialized or
  reformatted.
- `needs_hw`: reject. The changed test cannot run as a host program: it needs
  a board, MCU peripheral, radio, sensor or other device, or another
  simulator.

The test change itself must detect the behavior. A production fix that an
unchanged or merely reformatted test happens to exercise does not qualify: if
the PR's diff of the test package adds no case and changes no setup, input or
assertion that the old code violates (a reformatted helper, a renamed variable,
initialized locals, a removed setting), the kind is `maintenance` even when the
suite runs the fixed code path.

A fix without a detecting test cannot be graded. Test maintenance without a
production behavior to implement is not a coding task. Judge by the changed
assertion, never by the title or file names.

## 2. Find the fix

For the first two kinds the fix is the PR's own production change. Separate
it from the test change with the PR's per-file diff and, when there are
several commits, `commit_diff` (which commit changed tests, which changed
production code). Production code is everything outside the unit-test
packages; a change to a test package's own `pkg.yml`, `syscfg.yml` or helper
is test-side.

For a PR that looks test-only, search for the earlier change. Useful evidence,
strongest first:

1. **Explicit references** in the PR body, commit messages, comments, and
   review comments: PR or issue numbers, URLs, commit hashes, `MYNEWT-nnn`
   ids. Open each with `open_ref`; a commit shows the PR it belongs to.
2. **Symbols and behavior**: read what the changed test calls and asserts.
   Use `git_log` with `paths` on the production files that implement it,
   `regex` or `symbol` for the functions, constants and error codes involved,
   `until` the PR's base date (and `since` a few months to years earlier).
   `symbol` finds commits that change how many times a string occurs and
   misses a fix that edits a line containing the name without adding or
   removing it: also search with `paths` and `regex`.
3. **Commit messages** of the subsystem, with `message`.

Confirm from the diff: open the candidate and say why the changed test fails
on the code before it and passes after it. Read the production code as it was
at the PR's base (`read_file` at `base`, or at the commit before the
candidate) and at the PR's head. If the test also passes on the code before
the candidate, it is not the fix. If you searched and found nothing that the
test detects, the kind is `maintenance`; do not invent a fix. A test of
behavior that has always worked is `maintenance`.

The candidate must be a change merged before the PR's base. Give its PR
number if it has one (`fix_pr`) and a commit that contains it (`fix_commit`,
a full or short SHA from `open_ref`).

## 3. What we can run

The runner builds one package on the host with `hw/bsp/native` and
`compiler/sim`:

```bash
newt test @apache-mynewt-core/<test_path>
```

`test_path` is the directory of the unit-test package (it contains `pkg.yml`
with `pkg.type: unittest`), not a file. The generated binary is executed
directly and the runner reads the testutil lines `[pass] suite/testcase` and
`[FAIL] suite/testcase`. A missing, unexpected or crashed testcase invalidates
an instance, so the inventory below must be exact. Hosts are x86 Linux; before
2024 the old native BSP is a 32-bit build. QEMU is not available. Old
packages (2016-2019) live in `test/` or `test-*` directories instead of
`selftest`; they are valid unit-test packages, and an old `main()` may return
0 even when cases fail, so the inventory of `[pass]`/`[FAIL]` lines decides,
not the exit status.

Reject as `needs_hw` a package whose dependency chain needs real hardware: BLE
controller or radio, MCU HAL or peripheral drivers, UART/SPI/I2C devices.
Packages for flash, sensors, filesystems and memory that run on the native
BSP's simulated devices are runnable: check the package's `pkg.yml`
dependencies and the test's own source before deciding. Do not decide from a
`hw/` path alone.

`failure_mode`:

- `runtime` (default): the test builds on the old code, a named
  `[FAIL] suite/testcase` appears without the fix and `[pass]` with it.
- `compile`: a changed test source refers to a production interface the old
  code lacks, so the old code does not build. Then give `compile_test_source`
  (the changed C source) and `missing_api` (the exact identifier or header as
  written in it). All other build failures are not a valid oracle.

Determinism matters. A test that opens real host sockets or talks to a host
network interface, or whose pass/fail depends on a wall-clock timeout, thread
scheduling or a race (waiting on a semaphore or event with a tick timeout for
another task or an external peer), is not a reliable oracle on a shared build
host, even when the fix and the changed assertion look correct. For such a
PR set `confidence` to `low` (a recorded lead), whatever the kind. If the PR
is mainly about making that test synchronize or pass reliably, the kind is
`maintenance`.

## 4. Test and fix must separate

Applying only the test changes to the old code must leave the production bug
in place. If the PR has several commits, the test and its support may land
before the fix: inspect the per-commit diffs with `commit_diff`. Set
`test_commit` to the last commit that contains only test changes (and any
support), and `test_support_paths` to the paths outside the unit-test package
that this commit changes only to make the tests executable (for instance a
test-only accessor or a declaration). Never list the production fix there.
Omit both when no commit boundary is needed. If the production diff overlaps
lines the test-only patch changes, `gold_patch_three_way` is `true`.

`compatibility_cflags` are infrastructure-only compiler flags an older base
needs to build with a current compiler (e.g. a warning that became an error).
Usually `[]`. Never use them to hide a failure the missing change causes.

## 5. Inventory

Your input ends with the testcases computed from the package sources at the
PR's head, as `suite/testcase`, one list per package. Use these names exactly:

- `fail_to_pass`: testcases that fail on the old code (with the test patch
  applied) and pass with the fix. Trace each one: take the case's own inputs
  through the old production code and name the assertion that fails. A newly
  added case that targets the bug is the usual entry. An existing case that
  the PR only modified belongs here only if the modified lines themselves
  contain the assertion or input the old code violates; when a new case
  already covers the bug and the modified case merely shares helper code or
  gained unrelated lines, list the modified case in `pass_to_pass`. The
  trigger condition (the input, size, sequence or state that exposes the bug)
  must occur in that case's own body and parameters: an extra generic check
  added to a case whose own inputs never reach the bug does not make it fail.
  Cases added in the same PR as control or baseline scenarios (the simple
  configuration, the case where only one resource or path is involved, the
  case that exercises the behavior that already worked) pass on the old code
  too: they belong in `pass_to_pass` even though they are new.
  A wrong `fail_to_pass` entry invalidates the whole instance while an omitted
  one only loses a check, so be conservative. A case that only exists to exercise unchanged
  behavior, or only changed in an unrelated way, is not here.
- `pass_to_pass`: every other testcase of the package: unchanged cases and
  added cases that do not detect the change. A case is in exactly one of the
  two lists.
- `baseline_tests`: testcases present and passing before the test patch is
  applied (usually `pass_to_pass` minus newly added cases; empty for a new
  package).

If the PR changes several packages, pick the one containing the detecting
test and use only its inventory.

## 6. Problem statement

`problem_statement` is what the agent sees. If the PR links an issue, use the
issue's text (trim noise). Otherwise write one from the PR title, body,
commit messages and the changed test's assertion: the observable symptom, the
conditions that trigger it, and the behavior required. Never include the fix:
no function to edit, no source location, no code from the production diff, no
description of the implementation. For `feature_with_test` and compile-mode
tasks, name the interface the test needs (names and behavior), since the agent
cannot guess it. For a test-only PR, describe the behavior from the test and
the earlier fix's symptom, without the earlier fix's code.

## 7. Confidence

- `high`: the production change and the detecting assertion are both
  confirmed from the diffs, the package is host-runnable, and the test and
  fix separate cleanly.
- `medium`: the change plausibly makes the changed test fail before and pass
  after, but one thing is unconfirmed (an indirect link, how the test is
  separated, a platform detail). Prefer `medium` over `low` when the fix
  plausibly fits.
- `low`: a lead only: weak link, unclear oracle, or you could not confirm the
  fix. Reject kinds carry a confidence too; use `high` when sure.

## 8. Tools

- `read_file`, `list_dir`, `grep` read `apache/mynewt-core` at `base` or
  `head` (the PR's commits), any commit SHA, or as of a date `YYYY-MM-DD`
  (the default branch at the end of that day).
- `commit_diff` shows what one commit (any SHA) changed.
- `git_log` searches default-branch history, newest first, by dates, symbol,
  regex, message and paths.
- `open_ref` follows a PR, issue, commit URL or commit SHA.

The tool budget is limited. Start from the record and the diffs, then explicit
references, then symbols and dates. Once you have what you need, answer; do
not spend the rest of the budget. Do not use outside knowledge about specific
PRs.

## 9. Output

Return exactly one JSON object and nothing else.

Accept (any of the three accepting kinds):

```json
{
  "pr": 1234,
  "kind": "fix_with_test",
  "confidence": "high",
  "reason": "What the PR changes and why it is this kind.",
  "searched": ["PR diff: production change in lib/x/src/x.c", "git_log paths=lib/x until 2022-01-01"],
  "test_path": "lib/x/selftest",
  "failure_mode": "runtime",
  "fix_pr": 1234,
  "fix_commit": "<sha of a commit that contains the fix>",
  "fix_paths": ["lib/x/src/x.c"],
  "fail_to_pass": ["x_test_suite/x_test_case_new"],
  "pass_to_pass": ["x_test_suite/x_test_case_old"],
  "baseline_tests": ["x_test_suite/x_test_case_old"],
  "detecting_test": "Name the testcase and the assertion and explain why it fails on the unfixed code and passes on the fixed code.",
  "problem_statement": "Observed symptom and required behavior, without the implementation.",
  "test_commit": null,
  "test_support_paths": [],
  "compatibility_cflags": [],
  "gold_patch_three_way": false
}
```

Reject (`maintenance` or `needs_hw`): only `pr`, `kind`, `confidence`,
`reason` and `searched` are needed.

Field rules:

- `pr`: the record's `number`, an integer.
- `kind`: one of the five kinds above. The three accepting kinds are accepts;
  `maintenance` and `needs_hw` are rejects.
- `test_path`: the unit-test package directory, one of the record's test
  package roots.
- `fix_pr`: the PR number whose production change is the fix: this PR's number
  for `fix_with_test` and `feature_with_test`; an earlier merged PR for
  `test_only_detects_earlier_fix` (`null` if the fix has no PR).
- `fix_commit`: for `test_only_detects_earlier_fix`, a SHA of the earlier fix
  commit (the last commit of the fix when it spans several). Omit otherwise.
- `fix_paths`: production files the fix changes.
- `fail_to_pass`, `pass_to_pass`, `baseline_tests`: `suite/testcase`
  identifiers, exactly the form of the computed inventory. `fail_to_pass` is
  not empty.
- `test_commit`, `test_support_paths`: only for a PR in which test setup lands
  before the fix (section 4). `test_commit` is a full SHA of one of the PR's
  commits.
- For `compile` mode add `compile_test_source` and `missing_api` (section 3);
  `fail_to_pass` then names the tests expected to pass once the interface
  exists.
- Do not invent links, paths, names, target support, flags or results.
