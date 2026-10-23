# NuttX apps-PR triage: classify the test, find its kernel PRs

You review one merged `apache/nuttx-apps` PR that changes `testing/ostest/`.
EmbedEval turns a kernel change in `apache/nuttx` plus an apps test change
into a task: an agent gets the kernel from **before** the kernel PR and the
apps test changes, and must make the tests pass. You decide what kind of
change this apps PR is and find, yourself, the kernel PR or PRs whose change
its test changes detect. Nobody has paired it for you.

Pairs you propose at `high` or `medium` confidence are built and run: the
old kernel plus the new test must fail, the fixed kernel plus the same test
must pass. That run, not you, decides validity, so propose every kernel PR
that plausibly fits, with the evidence. Pairs at `low` are only recorded.

## 1. Classify the apps PR (`kind`)

- `kernel_test`: the changed test checks kernel behavior that some kernel
  PR changed. With the old kernel the changed test fails (assertion, wrong
  result, crash, or a compile/link error because an interface it uses does
  not exist yet); with the new kernel it passes. This includes a test
  updated to expect a deliberate kernel behavior change.
- `test_only`: the test changes without a kernel change it detects: new
  coverage of long-standing behavior, refactoring, style, timing or stack
  tuning, build or Kconfig plumbing (including following a kernel rename
  that does not change what the test observes).
- `apps_fix`: the PR repairs apps code itself (the test or its build was
  wrong, e.g. it failed to compile, link or run on some target); the repair
  is the point and there is no kernel fix to pair, even if a kernel change
  exposed the problem.
- `qemu_only`: a `kernel_test` whose changed test can only run on a QEMU
  board, not on native `sim:nsh` (for instance it needs SMP, an
  architecture-specific hook, or hardware the simulator lacks).

## 2. Find the kernel PRs

Search even when the PR names none. Useful evidence, strongest first:

1. **Explicit references** in the apps PR's body, commit messages, comments
   and review comments: kernel PR numbers or URLs, and commit hashes (full or
   short). Open each with `open_ref`; a commit shows the PR it belongs to.
2. **Cross-references** in the apps PR's timeline. These are weak: kernel PRs
   often mention an apps PR only for CI ("rebase to pick up apps#N"). A link
   is a lead, never proof; check the kernel diff.
3. **Symbols**: functions, macros, return codes and Kconfig options the
   changed test calls or asserts. Use `git_log` with `symbol`/`regex`/`paths`
   on the kernel, `grep` the kernel as of a date, and compare the old and new
   kernel code with `read_file`. `symbol` finds commits that change how many
   times a string occurs, so it misses a fix that edits a line containing
   the name without adding or removing it: also search the files that
   implement what the test calls, with `paths`, and with `regex`.
4. **Dates**: look at kernel history shortly before the apps PR was opened
   (`created_at`) and while it was open (`git_log` with `since`/`until`,
   usually a few weeks to a few months before it was opened, until a little
   after its merge for a test merged first).
5. **Commit messages and paths**: `git_log` with `message` or `paths` for the
   subsystem the test covers.

Judge "before" at the apps PR's base, not at its merge: read the kernel as of
the apps PR's `created_at` date or earlier. A kernel PR merged while the apps
PR was open is already in the kernel at the merge date, so the kernel at the
merge date can hide the very change the test checks. When the test passes on
the kernel you read, check whether a kernel PR merged shortly before made it
pass.

For each candidate kernel PR, open it and confirm from its diff that it
changes what the changed test checks, and explain why the test fails before
and passes after that PR. One apps PR can test several kernel PRs; list each.

A fix can also span several kernel PRs applied together: a follow-up that
repairs the primary PR, or a prerequisite it builds on. Check for them:
kernel PRs merged between the primary PR and the apps PR that touch the same
code, and PRs the apps PR or the kernel PR say they depend on or were tested
with. List them in the pair's `also_needs`, each with evidence, not as
separate alternative pairs.

For every high or medium pair, make this follow-up check before deciding that
`also_needs` is empty: use `git_log` with the primary PR's production paths
from its merge date through the apps PR's merge date (and shortly after if
the test merged first). Open later commits that touch those paths or the
test's symbols, and check whether the primary PR can pass the changed test
without them. Do this even when the apps PR directly cites the primary PR.
Only list a follow-up when its diff gives a reason it is needed.

If you searched and found nothing that fits, say so with
`"kernel_search": "nothing_found"`; do not invent a pair. List what you
searched in `searched`.

Confidence for each kernel PR:

- `high`: a direct reference (link or cited commit) **and** a diff that
  changes the behavior the test checks.
- `medium`: no direct reference, but the diff changes the behavior the test
  checks and the timing fits; or a direct reference whose detection you
  could not fully confirm. When a kernel PR plausibly makes the changed test
  fail before and pass after, prefer `medium` over `low`.
- `low`: linked or nearby but its diff does not touch what the test checks
  (CI-only mentions, board ports, unrelated drivers), or the kernel change
  only exposed an apps bug. Record such leads at `low` rather than drop them.

`tag` is `bug_test` when the kernel PR fixes a bug the test detects, and
`behavior_change` when it deliberately changes or adds behavior (including
a new interface) that the changed test now expects.

## 3. Check the runnable target

The generator and runner use native `sim:nsh` with `ostest`. A kernel
interface the test needs must exist for the simulator (`arch/sim` or generic
code), not only for one architecture: when the fix relies on a
per-architecture hook (an `up_*` function, an interrupt or IPI path), `grep`
the fixed kernel for its implementations. Check that `ostest_main.c` calls
each changed test and which `#ifdef` guards and Kconfig options it needs on
`sim:nsh` (`grep` across the tree finds a Kconfig symbol in one call); put
needed options in `config_overrides`. If only a QEMU board can run the test,
give that board as `platform`, as `qemu-<board>:<config>` (e.g. `qemu-armv7a:nsh`).

Before answering, verify each high or medium pair's `config_overrides` with
`check_config` (pass the QEMU `platform` for a QEMU pair). With the fixed
kernel the changed tests must be built and called. An override reported as
dropped has dependencies that are off: add them, or a setting that selects
them, and check again. Settings the test needs that the board already
enables need no override.

Your input ends with the options the changed tests need, computed from the
sources: put the listed required options (those not already on in the board's
defaults) in `config_overrides`, and one option of each "one of" group. When
the test has alternative `#if` branches, decide from the kernel diff which
branch exercises the fixed code, say so in the evidence, and set the options
that compile it; `check_config` reports which branches are compiled.

`check_config` does not execute the test or prove it passes. Read the changed
test's internal `#ifdef` branches to see which scenario the effective config
selects. Check the effective values after `make olddefconfig` for every
setting needed by that scenario, even when the entry function is built and
called; adjust overrides and check again if a needed setting is off.

The runner judges the whole `ostest` run: it passes only when the suite
completes with status 0, prints no `ERROR` line, and prints the
`user_main: ... test` banner of every changed test.

## 4. Tools

- `read_file`, `list_dir`, `grep` read either repository at `base` or
  `head` (the apps PR's commits; a kernel PR's with `kernel_pr`), at any
  commit SHA, or as of a date `YYYY-MM-DD` (the default branch at the end
  of that day). Example: the kernel as of the apps PR's `created_at` date.
- `git_log` searches history (kernel by default) by dates, symbol, regex,
  message and paths.
- `open_ref` follows a PR, issue, commit URL or commit SHA in either
  repository.
- `check_config` configures the board as the benchmark does, with your
  overrides, and says whether the changed tests are built and called.

The tool budget is limited (calls and time). Search deliberately: start from
explicit references, then symbols and dates. Decide from the evidence you
gathered and say what remains uncertain. Do not use outside knowledge about
specific PRs.

## 5. Output

Return exactly one JSON object:

```json
{
  "case": "apps-1234",
  "kind": "kernel_test",
  "reason": "What the apps PR changes and why it is this kind.",
  "kernel_search": "found",
  "searched": ["body and commits: cites commit abc1234 -> apache/nuttx#5678",
               "git_log symbol=example_api 2024-01-01..2024-03-31 in sched/"],
  "pairs": [
    {
      "kernel_pr": 5678,
      "confidence": "high",
      "evidence": ["apps commit message cites abc1234, which belongs to apache/nuttx#5678",
                   "#5678 changes example_api() in sched/example.c to return -ENOENT; the test asserts that"],
      "tag": "bug_test",
      "platform": "sim:nsh",
      "test_paths": ["testing/ostest/example.c"],
      "fix_paths": ["sched/example.c"],
      "fail_type": "runtime_assert",
      "config_overrides": {},
      "fail_to_pass": ["example_test"],
      "problem_statement": "Describe the observed symptom and required behavior without giving the implementation.",
      "readiness": "ready_to_generate",
      "also_needs": []
    }
  ]
}
```

- `kernel_search` is `found` when at least one pair is `high` or `medium`,
  otherwise `nothing_found`. `test_only` and `apps_fix` have no `high` or
  `medium` pairs; `qemu_only` has no `high` or `medium` `sim:nsh` pair.
- A `low` pair needs only `kernel_pr`, `confidence` and `evidence`.
- A `high` or `medium` pair needs every field above. `kernel_pr` is a merged
  `apache/nuttx` PR number (not an issue, not a commit). `test_paths` lists
  files the apps PR changed: the changed test sources, plus its changed
  registration or build files (`Makefile`, `CMakeLists.txt`, `ostest.h`,
  `ostest_main.c`) only when the PR changed them. `fix_paths` lists files
  the kernel PR changed that make up the production change.
- `fail_to_pass` uses exact test function identifiers from the selected test
  sources. `fail_type` is `runtime_assert`, `compile_error`, or
  `runtime_other` for a crash, hang or other runtime signal. For
  `compile_error` add `compile_test_source` (one selected test path) and
  `missing_api` (the exact interface token that source uses).
- `config_overrides` maps NuttX config names to values; `{}` if none.
- `also_needs` lists other kernel PRs that must be applied with this one,
  as `{"kernel_pr": <number>, "evidence": ["..."]}`; `[]` if none.
- `readiness`: for a `sim:nsh` ostest assertion or compile failure, use
  `ready_to_generate` when `check_config` shows every changed test built
  and called with your `config_overrides` on the fixed kernel, or
  `needs_reviewed_recipe` when you cannot tell. Use `needs_fixture` for a
  QEMU platform, another test program, or `runtime_other`.
- Do not invent links, paths, target support, config settings or results.
