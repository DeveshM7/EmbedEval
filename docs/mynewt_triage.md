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

This is source-based triage, not a validation result. Passing the hard filters
only establishes a merged, non-revert PR touching a unittest package or test
support; it does not establish an executable regression. Do not emit an
exploratory package with an invented `fail_to_pass` just to give every PR a run.
Unresolved accepting kinds belong at `low` confidence with the missing proof in
`reason` and `searched`; they are leads, not validated tasks.

This document is the complete model-facing triage procedure. Inspect the exact
candidate revisions; current upstream layout and previous successful candidates
are navigation aids, not evidence that this candidate is runnable or detects a fix.

PRs you accept at `high` or `medium` confidence are generated and run: the
old code plus the new test must fail, the fixed code plus the same test must
pass. That run, not you, decides validity. Accept plausible candidates only with
a source-grounded detecting test and a concrete execution profile. Do not claim
an unexecuted prediction was observed. Accepts at `low` are only recorded.

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
If the old code already fails at an unchanged assertion before reaching the
new test lines, those new lines do not detect the fix. Changing `main()` to
return a failure status does not create a new failing testcase line.

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

Use the Linux x86_64 profile: `hw/bsp/native` with `compiler/sim`.
The model-serving GPU architecture is unrelated to the test worker architecture.
Do not propose an ARM MCU board, QEMU board, or an unvalidated aarch64 simulator
port as an interchangeable target. The runner builds one package:

```bash
newt test @apache-mynewt-core/<test_path>
```

`test_path` is the directory of the unit-test package (it contains `pkg.yml`
with `pkg.type: unittest`), not a file. The generated binary is executed
directly and the runner reads the testutil lines `[pass] suite/testcase` and
`[FAIL] suite/testcase`. The complete inventory must be accounted for. A crash
is not proof that every remaining case executed and failed. If a crash oracle needs a reviewed debugger
observer, identify the active case and source attribution; keep unexecuted cases
explicitly separate and leave confidence low until that observation is available.
Hosts are x86 Linux; before the April 2024 simulator changes, old native builds
use 32-bit x86. QEMU is not available. Old
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

### Repository and toolchain facts

`kernel/` contains OS services; `hw/` contains BSPs, native/MCU support and
drivers; `compiler/` contains toolchain definitions; `test/testutil` contains
the test framework. Production and test packages also live under `sys/`,
`util/`, `fs/`, `encoding/`, `libc/` and `net/`. Directory names do not establish
hardware requirements or test runnability.

For the selected revisions, inspect:

- `pkg.yml`: actual package type, dependencies (including conditional keys),
  required/provided APIs, and initialization requirements.
- `syscfg.yml`: selected values, feature guards and restrictions. Read both
  production defaults and test overrides; conditional APIs may disappear.
- `repository.yml`: external repository/version mappings. An external package
  needs a compatible pinned revision and builder support, not an arbitrary HEAD.
- `hw/bsp/native/{pkg.yml,bsp.yml}`, `compiler/sim/compiler.yml`, and test
  entry/registration sources: the executable target and active testcase set.

Newt is the build tool from the separate `apache/mynewt-newt` repository;
`test/testutil` is the framework inside `mynewt-core`. The builder selects a
pinned historical Newt binary for the candidate base. Old trees may need
32-bit libraries and reviewed modern-compiler compatibility flags. Missing
multilib, an incompatible Newt, or an absent external package is not automatically
the selected regression. Do not repair it by weakening assertions or importing
the target production fix into the before state.

Static testcase macros are an inventory lead, not proof of execution. Trace
suite registration, preprocessor conditions and test configuration. Newt must
resolve the actual dependency/configuration graph, and validation must establish
the named runtime results. There is no Twister-style compatibility table in this
workflow that makes those checks unnecessary.

### Selection and execution readiness

Before selecting a package, perform these checks and summarize evidence in
`reason`, `searched`, and `detecting_test` (existing output fields):

1. Read its `pkg.yml` at base and test revisions. It must be `pkg.type: unittest`.
   A `lib` under `selftest/` is shared support, not a runnable target. Trace its
   consumers to an actual unittest package. The current triage checker requires
   `test_path` in the enriched roots; if the proven consumer is absent there,
   do not emit an accepting verdict with that consumer, even at low confidence:
   it cannot pass the current checker. Name the missing consumer/root evidence
   in the JSON `reason`/`searched` explanation; any resulting checker failure
   is retained for out-of-band input review, not scored as a semantic rejection. This needs enrichment or explicit manual
   review before triage can continue; do not substitute the helper or mislabel
   the PR as maintenance or hardware-dependent. A schema error is an unresolved
   input/representation problem, not evidence the PR is unsuitable.
2. Trace `pkg.deps` including conditional keys, `pkg.apis`, `syscfg.yml` values
   and restrictions, test entry/registration, and external repository references.
   Cite the actual dependency chain that makes native execution possible or
   requires hardware. Newt resolves the configuration; a source read alone is
   not a successful build. The native BSP includes simulated flash/TRNG and host
   sockets: those subsystem names do not themselves imply physical hardware.
3. Read the selected testcase's changed inputs/assertion and the old production
   implementation. Explain the trigger and expected failure for each proposed
   `fail_to_pass` case. A shared helper, newly added case, or missing function
   name alone does not establish runtime failure. If the expected failure is
   compilation, select compile mode and identify the source/API instead.
4. Check that dependencies and the selected package exist at the appropriate
   revisions. A new unittest package may legitimately be absent at baseline:
   `baseline_tests` is then empty. It must exist after the test patch, without
   importing the target production fix. Do not reject solely because old tests
   fail for another reason; do not accept that unrelated failure as detection.
5. Record host/tool requirements and blockers in `searched`. Use the Newt binary
   selected for the exact base revision by the renderer; do not guess from the
   PR date or use the newest binary for every historical tree. Missing compiler,
   incompatible worker, and output-decoding errors are infrastructure failures.

Network and scheduling require evidence, not a blanket path-based exclusion.
A self-contained parser test may need no network despite depending on a socket
package. A test requiring interfaces may use only a reviewed, reproducible local
fixture: private network namespace, explicit interfaces/addresses, no external
peer, DNS, credentials, or Internet access. EmbedEval's existing `mynewt-veth`
fixture supplies `bench0`/`bench1`; do not assume that fixture fits a new PR just
because it helped another. The normal generator does not propagate a
`network_fixture` verdict field; fixture-dependent candidates need a separately
reviewed validation wrapper/metadata handoff. They are not ready for the normal
batch path merely because the agent harness supports that fixture. If the record does not establish a supported fixture,
leave confidence low and name the requirement. Do not invent a JSON metadata key
or silently enable network access. Timing/race tests need a source-grounded
synchronization argument and repeated execution evidence before promotion; a
longer timeout alone is not evidence of determinism.

Do not add simulator compatibility fixes during triage. A separately reviewed
fixture must be pinned, disjoint from the target fix/test changes, protected,
and identical in baseline/before/after. If needed but unavailable, name it as a
blocker. Never backport the target fix into the starting image to make it build.

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

`simulator_fixture: "taskpool-stack-2150"` applies the exact separate upstream
taskpool stack-size correction for the old native simulator. It requires the
source file to match the pinned upstream parent and be disjoint from the selected
fix and test patch. The fixture is committed before the test snapshot, retained
in baseline/before/after, protected from agent edits, and recorded with source
commits and a patch hash in host metadata. It is not part of the target PR's gold
patch.

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
  When several cases reach the same new assertion with different inputs,
  trace each case separately through the old code, including sizes and
  boundary conditions. If you cannot show why the old code fails for a
  modified case's own input sequence, put it in `pass_to_pass`; sharing an
  assertion with a failing case is not enough.
  Cases added in the same PR as control or baseline scenarios (the simple
  configuration, the case where only one resource or path is involved, the
  case that exercises the behavior that already worked) pass on the old code
  too: they belong in `pass_to_pass` even though they are new.
  For example, two fill tests can share a reinitialization assertion while
  only the smaller-write sequence reaches the buggy metadata boundary. Trace
  sizes and state for each; do not label both failing from a shared assertion.
  A wrong `fail_to_pass` entry invalidates the whole instance while an omitted
  one only loses a check, so be conservative. A case that only exists to exercise unchanged
  behavior, or only changed in an unrelated way, is not here.
- `pass_to_pass`: every other testcase of the package: unchanged cases and
  added cases that do not detect the change. A case is in exactly one of the
  two lists.
- `baseline_tests`: testcases present before the test patch is applied
  (usually `pass_to_pass` minus newly added cases; empty for a new package).
  Their results diagnose original-code health; a baseline failure alone does
  not reject an instance. Acceptance requires a failure attributable to the
  upstream production fix before that fix, followed by a pass afterward.

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
  confirmed from the diffs, the runnable unittest and native dependency chain
  are identified, the exact testcase trigger is explained, and the test and fix
  separate cleanly. This remains a source prediction until validation runs.
- `medium`: the change plausibly makes the changed test fail before and pass
  after with a runnable package and a concrete source-level causal argument,
  but a bounded execution detail remains unconfirmed. State it explicitly.
  An unknown runnable target, unknown external dependency, missing fixture, or
  unproven detecting assertion requires `low`, not a speculative executable plan.
- `low`: a lead only: weak link, unclear oracle, or you could not confirm the
  fix. Reject kinds carry a confidence too; use `high` when sure.

Before returning an accept, check: exact revisions; real unittest root;
base-compatible Newt/x86 profile; dependency/fixture requirements; protected
upstream test patch separate from production fix; per-case causal argument;
disjoint and complete fail/pass lists; baseline inventory; honest uncertainty.
The JSON is a candidate specification. Only saved execution evidence can call
it a validated instance.

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

### Optional fields and reviewed-only handoffs

Keep the ordinary runtime/changed-C-source compile output above as the default.
These optional fields are not interchangeable; use only existing source or saved
observations, never invented logs, hashes, API names or fixture metadata:

| Situation | Fields / handoff | Current boundary |
|---|---|---|
| New test package / renamed baseline package | `baseline_tests: []` when absent; optional `baseline_test_path` for a proven original package | Record the actual original inventory, not head names copied into baseline |
| Supported debugger observer | `test_observer: "testutil-gdb"` | Generator checks upstream event symbols; it does not by itself prove crash attribution |
| Configuration failure | `failure_mode: "config"`, `config_expectation` | Requires saved exact output and repository-relative `observed_log`, plus setting/path proof; source-only triage cannot invent these |
| New production dependency | `failure_mode: "dependency"`, `change_type: "feature_with_test"`, `dependency_expectation` | Required in addition to the accepting `kind`; requires provider/kind and saved exact output/`observed_log` checked by generator |
| Multiple compile diagnostics / test-helper include | `compile_expectations`, optional `compile_dependency_headers` | Reviewed generator path; unchanged helper/header evidence does not satisfy the ordinary triage checker's changed-C-source rule |
| Existing API provider retained equally in states | `base_api_providers` | Reviewed generator proof, not permission to add the target fix as a dependency |
| Pinned external repository | `mcumgr_commit` where supported | Exact compatible revision; generator checks upstream release mapping |
| Simulator compatibility | `simulator_fixture` where supported | Reviewed renderer/metadata handoff, not a new model-invented fixture |
| Local network topology | Separate reviewed validation wrapper and agent metadata | Normal verdict generation does not wire `network_fixture` through |

Config/dependency profiles without saved execution evidence, helper consumers
outside enriched roots, and compile cases outside the checker's source rule must
remain unresolved/manual-review items. They cannot be made executable by adding
a plausible-looking JSON object. The reviewer must save the evidence and correct
the representation before the generator handoff.

The current runner can label all unreported cases as failed after a signal.
Those are crash-inferred labels, not individual observations. Require separate
observer/trace evidence for the active detecting case and preserve the list of
unexecuted cases. Do not promote a newly triaged crash task based on the apparent
completeness of that inferred list; it needs a reviewed evidence/representation
handoff. This prompt does not change the runner's classification algorithm.

### Source-attributed configuration failures

A reviewed `failure_mode: "config"` records a Newt restriction failure before
compilation, separately from a runtime assertion failure. `config_expectation`
contains the exact observed Newt output, `production_path`, `setting`,
`test_config_path`, and `test_settings`. Generation verifies that the real PR
changes that production setting's value or restrictions and that the protected
upstream test configuration contains the specified inputs. The runner accepts
only the fingerprint of the complete observed build output; other errors remain
inconclusive. After applying the production fix, the entire named testcase
inventory must execute and pass. No individual testcase is reported as having
executed before a configuration failure. This does not accept generic
missing-library errors.

For a missing include in an unchanged protected test helper,
`compile_dependency_headers` can identify its API header, existing provider,
changed production package file, selector flag, and changed test configuration.
Generation requires identical helper content at base/test, a direct include,
an existing provider/header at base, a newly added conditional dependency in
the production fix, and selector value 1 in the upstream test. This attributes
an include-path failure without copying the dependency fix into the before
state. All actual compiler errors must still match the expectation profile.

A scoped production sub-fix can retain a legacy test dependency through
`base_api_providers`. The provider must be listed in the original test package,
provide an API required by the named base consumer, and both provider and
consumer must remain outside the selected production/test patches. The builder
adds that existing provider to the generated target in both phases; it never
copies implementation code from the fixed revision into the before state.

### New-module functionality additions

`failure_mode: "dependency"` is reserved for `change_type: "feature_with_test"`.
The exact observed Newt dependency-resolution output is fingerprinted. The
provider package must be absent at base, selected in the upstream production
patch, and directly required by the protected test package. In-repository short
package names are equivalent to their `@apache-mynewt-core/` names. For a missing
API provider, source checks instead require the test's base consumer to require
that API and the upstream BSP change to select the newly added provider.
These are build-failure-to-named-pass tasks: no testcase is claimed to execute
before dependency resolution succeeds. The full named inventory must run and
pass after the real upstream implementation is applied. Keep this label when
exporting the tasks so they can be separated from existing-code bug fixes.
