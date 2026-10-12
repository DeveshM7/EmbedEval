# NuttX PR-pair triage

Review the supplied Apache NuttX PR pair as a possible EmbedEval task. The
`apache/nuttx` PR supplies the production fix. The `apache/nuttx-apps` PR
supplies a changed executable test. The pair may have been found by a link,
search, or a human; that pairing is only a lead. This prompt does not discover
partners. A score on supplied pairs does not measure discovery recall.
Some legacy gold records contain several kernel leads or no apps PR. Select
one supplied kernel PR for an accept; reject a kernel-only record because it
has no changed apps test for this pair workflow.

## The decision

The agent would receive the kernel code from **before** the fix plus the apps
test changes. The benchmark then applies the same apps test changes to both
the old and fixed kernel. Accept a promising pair only when you can explain
both halves of this question:

> Which changed, runnable test detects the kernel behavior, and why should it
> fail with the old kernel but pass with the fixed kernel?

A new test can fail during compilation if it calls an interface introduced by
the kernel fix. An assertion, nonzero exit, crash, or timeout can also detect
a regression. The failure must belong to the proposed bug, not to missing
infrastructure or a test that skips. A test that only changes guards, stack
size, timing, or its own implementation is not enough.

## How to review the record

1. Name the kernel production hunk and the apps test function or assertion.
   Check that the apps PR actually changes the test, registers it, and enables
   it in the selected configuration. Include registration/configuration files
   in `test_paths` when the test patch needs them.
2. Compare the apps test with the kernel code at its base and head commits.
   Explain the expected old failure and fixed pass using those exact PR
   boundaries. Check whether the apps test can apply to the old apps revision.
   Both PRs must be merged and have base/head branch commits. A passing
   upstream CI run alone does not prove this comparison. Compare merge timing
   for context, but use the actual branch commits to define the two revisions.
3. Check that the test runs on native `sim:nsh`, the only target the
   generator and runner support. A kernel interface the test needs must be
   implemented for the simulator itself (`arch/sim`, or generic code), not
   only declared, or implemented for one hardware architecture: when the fix
   relies on a per-architecture hook (an `up_*` function, an interrupt or IPI
   path), `grep` the fixed kernel for its implementations. If the pair is
   otherwise sound but its test can only run on a QEMU board, reject it and
   describe that board in `qemu_lead`. Other apps test programs on `sim:nsh`
   can be promising but need a fixture. Note required config settings and any
   hardware or external dependency.
4. Identify an issue if the record supplies one. Missing issue provenance and
   whether a feature qualifies are separate dataset-policy questions; do not
   reject a mechanically promising pair solely for either.

The record includes PR diffs, discussion, branch commit boundaries, changed
apps test contents under `testing/`, and kernel source before the fix when
available. Long fields can be truncated in the model input.
Use `read_file`, `list_dir` and `grep` on the apps or kernel repository at a
PR's base or head commit for a specific missing fact, such as test
registration, an `#ifdef` guard, or a config default (`grep` across the tree
finds a Kconfig symbol in one call). The tool budget is limited.
Decide from the available evidence and say what remains uncertain. Do not use
outside knowledge about these examples.

Reject if there is no credible kernel production change paired with a changed,
executable apps regression test, or if the test cannot run on `sim:nsh`. For a plausible pair, accept with lower confidence when
some detail still needs validation. Never report a runtime result you did not
observe. `evidence_status` is `observed` only when the supplied record has a
trustworthy old-kernel/new-test failure **and** fixed-kernel/same-test pass
trace; otherwise use `needs_validation`.

## Output

Return exactly one JSON object. Every result needs `case`, `verdict` (`accept`
or `reject`), and a specific `reason`. Copy the case ID from the record; the
example below uses an explicit pair ID, while gold cases use shorter IDs.
For an accepted pair, also include:

```json
{
  "case": "apps-2327-kernel-11889",
  "verdict": "accept",
  "reason": "The changed apps test checks the kernel behavior; runtime comparison remains to be run.",
  "kernel_pr": 11889,
  "confidence": "medium",
  "change_type": "fix",
  "test_paths": ["testing/ostest/example.c", "testing/ostest/ostest_main.c"],
  "fix_paths": ["sched/example.c"],
  "platform": "sim:nsh",
  "config_overrides": {},
  "problem_statement": "Describe the observed symptom and required behavior without giving the implementation.",
  "fail_to_pass": ["example_test"],
  "detecting_test": "example_test asserts the required result; explain the old failure and fixed pass on sim:nsh.",
  "fail_type": "runtime_assert",
  "readiness": "needs_reviewed_recipe",
  "evidence_status": "needs_validation",
  "issue_url": null
}
```

- `test_paths` and `fix_paths` must be changed paths in the respective PRs.
  Include only files needed for the test patch and production fix.
- `confidence` is `high`, `medium`, or `low`; `change_type` is `fix` or
  `feature`. Neither substitutes for the detecting test.
- `fail_to_pass` uses exact test function identifiers from the selected apps
  test sources. `detecting_test` names the relevant assertion and explains
  the causal path and target. `fail_type` is `runtime_assert`, `compile_error`, or
  `runtime_other` for a crash, hang, or other runtime signal.
- For `compile_error`, also give `compile_test_source`, one selected changed
  test path, and `missing_api`, the exact interface token that source uses.
  Do not infer either from a compiler log alone when the source is available.
- `config_overrides` maps required NuttX config names to their values; use
  `{}` when the record shows none. `platform` is `sim:nsh` for every accept.
- A reject may add `qemu_lead` when the only obstacle is the target: the pair
  has a real kernel fix and a detecting ostest test, but the test can only run
  on a QEMU board. Give `{"platform": "qemu-armv7a:nsh", "kernel_pr":
  <number>, "confidence": "high" | "medium" | "low", "why": "<what ties it
  to that board>"}`. Use `null` or omit it for every other reject.
- The runner judges the whole `ostest` run: it passes only when the suite
  completes with status 0, prints no `ERROR` line, and prints the
  `user_main: ... test` banner of every changed test. So every changed test
  must actually run on the selected configuration, and every other `ostest`
  test must still pass there.
- For a `sim:nsh` `ostest` assertion or compile failure, use
  `ready_to_generate` when `ostest_main.c` calls each changed test and
  `config_overrides` holds every setting those calls need on `sim:nsh`
  (check the `#ifdef` guards around the calls). Use `needs_reviewed_recipe`
  when you cannot tell whether a changed test runs there. Use
  `needs_fixture` for another apps test program or another failure mode. None of these statuses claims validation passed.
- `issue_url` may be null. Do not invent links, paths, target support,
  config settings, or test results.
