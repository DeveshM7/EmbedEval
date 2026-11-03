# OpenThread PR triage

You decide whether one merged OpenThread pull request can become a benchmark
instance, and if so which upstream test detects it. Your reply is a single JSON
object (section 6). Judgement is yours; names, options and patch application
are checked by code afterwards.

## 1. Task and policy

An agent under evaluation receives the repository at the PR's base commit with
the PR's own test changes applied, a problem statement, and a build/run command
that reports named test cases. It must change production code so the named
failing cases pass without breaking the others. The test files, including
`tests/unit/CMakeLists.txt` and `tests/gtest/CMakeLists.txt`, and the build
configuration are protected.

Policy for this port:

- **Upstream tests only.** The PR's own squash commit must change files under
  `tests/unit/` or `tests/gtest/`. Nexus, thread-cert, toranj, fuzz and expect
  tests are out of scope. Nothing is written for the benchmark: never edit an
  upstream assertion, never inject the fix into a test, never toggle unrelated
  options to manufacture a failure.
- **Runtime oracles only.** A case must fail (or crash) when run against the
  base production code with the new tests, and pass with the fix. If the only
  difference is a compile error (the test uses an API that only the fix adds),
  reject: "compile-only oracle".
- Bug fixes and small features are both fine (`change_type`). Linked issues are
  not required.

What the hard filters already prove (scripts/openthread_candidates.py): the
commit is a squash-merged PR (title ends `(#N)`), not a revert, changes files
under `tests/unit/` or `tests/gtest/`, and changes at least one file outside
`tests/`, `doc/`, `.github/`, `script/`, `third_party/`, `examples/`, `etc/`,
`tools/`, `zephyr/`. They do NOT prove that any test exercises the production
change, that the change is behavior (refactors, renames, comment edits pass),
that the test is compiled in the supported configuration, or that anything
fails before the fix. That is your job.

## 2. Repository map (revision 57368b5dcdf08445e5b974b0cf4382aba107ab52)

- Production: `src/core/` (the stack: `common/`, `net/`, `thread/`, `mac/`,
  `meshcop/`, `border_router/`, ...), `src/ncp/`, `src/lib/` (spinel, hdlc, url),
  `src/posix/`, public API in `include/openthread/`. Platform examples are in
  `examples/`, vendored code in `third_party/` (mbedtls is a git submodule since
  2025-11-12, vendored before).
- Unit tests: `tests/unit/test_<name>.cpp`, one executable each, registered in
  `tests/unit/CMakeLists.txt` by the macro `ot_unit_test(name [extra sources])`
  (target and ctest name `ot-test-<name>`, source `test_<name>.cpp`) or
  `ot_unit_ncp_test(name)` (`ot-test-ncp-<name>`), inside `if(OT_FTD)`; a few
  are plain `add_executable` + `add_test`. A file has its own `main()` that calls
  `TestXxx()` functions; `VerifyOrQuit`/`SuccessOrQuit` (`tests/unit/test_util.h`)
  print `FAILED` and exit on the FIRST failure. There are no per-case names:
  a unit test passes or fails as a whole executable.
- GoogleTest: `tests/gtest/*.cpp` (`TEST`, `TEST_F`, `TEST_P`), executables in
  `tests/gtest/CMakeLists.txt` (`ot-ftd-gtest`, `ot-posix-gtest`,
  `ot-radio-spinel-rcp-gtest`, ...), enabled by `-DOT_BUILD_GTEST=ON`, cases
  discovered by `gtest_discover_tests`. Per-case results exist. Before
  2026-10-23 that file had no `if(OT_FTD)`/`if(OT_RCP)` guards.
- Build options: `etc/cmake/options.cmake` (`ot_option(OT_X OPENTHREAD_CONFIG_X_ENABLE ...)`
  maps a CMake option to the config macro the sources test). Option names change
  over time; use the record's `ci_cmake_options`, which come from the base
  commit's own `.github/workflows/unit.yml`.
- The tools `read_file`, `list_dir`, `grep` read this repository; `ref` `base`
  is the base commit and `merge` (the default) is the fix commit.

## 3. Supported execution profile (UNVERIFIED until the smoke control runs)

Host-native CMake build in a container, no hardware or simulator: platform
`external` (no platform directory; unit tests link `tests/unit/test_platform.cpp`
and gtests link `ot-fake-platform`), FTD only, applications and RCP/MTD off,
Debug. Google Test comes from system packages. Which arguments exactly are set
is decided by the instance template; `cmake_options` in your verdict are only
additions to it. Source-predicted compatibility is not a successful build: until
the smoke control passes, none of this is observed. Only `suite` `unit` and
`gtest` targets that the record's inventory lists for an FTD build are supported;
targets requiring `OT_RCP` or `OT_MULTIPAN_RCP` (see `requires_options`) are not
part of the profile, so hold.

## 4. Selection rules

1. The detecting signal must come from the PR's changed test inputs: new or
   changed assertions, setup or helper code inside the test diff. A test that
   merely sits in a changed file, was only reformatted or renamed, or passed
   before the fix too, is not a detector. If the production change is a refactor,
   rename, logging or documentation, reject.
2. Trace it: name the changed test input, the old production path it reaches,
   the failure it produces on the base code (assertion, wrong value, crash),
   and how the fix changes it. Read the production diff and, with the tools,
   the surrounding source. A test hunk that cannot reach the changed code, or a
   test that would also pass on the base code, is a reject.
3. **Option-gated tests.** The CMake guards around a target are listed as
   `requires_options`. Separately, `#if OPENTHREAD_CONFIG_...` guards inside a
   changed test source are listed under "Preprocessor guards", mapped to the
   `OT_*` option that sets them: when such a guard is off, the test body is
   compiled out and the executable passes before and after. Check from
   `src/core/config/` whether the macro defaults to on. A needed option may be
   added in `cmake_options` only if it appears verbatim in `ci_cmake_options`
   (the base commit's own CI); otherwise hold. Never add an option to make a test
   behave differently from what CI builds. Note `ci_cmake_options` collects
   options of several CI builds, some of which conflict (`ON` versus `OFF`);
   choose consistently with what the target needs.
4. **Compile-only oracle.** If the test change only calls an API, type or enum
   that the production diff adds, and nothing it asserts could fail at run time
   on the base code, reject with reason "compile-only oracle".
5. **Unit tests are per executable.** Name the executable (`suite` `unit`,
   `target` `ot-test-<name>`); `fail_to_pass` is exactly that executable's ctest
   name, because it fails as a whole on the first failure. `pass_to_pass` may
   list other ctest names in the inventory that exercise related code (or `[]`).
   Executables with no case-level detail cannot be partially selected.
6. **GoogleTest** (`suite` `gtest`): `fail_to_pass` are the individual
   `Suite.Case` names whose result changes; `pass_to_pass` other cases of the same
   executable that exercise related code. Only names in that target's `cases`
   list are valid; do not guess names from the diff.
7. **Crashes.** A crash or abort on the base code counts as a failure: a unit
   executable that dies is `failed`; in a gtest binary the crashing case is
   `error` and later cases are not run, so cases that follow it in the same
   binary cannot be reliably pass-to-pass. Prefer a test that fails by assertion;
   a crash is acceptable for the case it kills.
8. If several executables are touched, choose the one that best detects the
   change; one target per instance.

## 5. Patch separation and problem statement

- `base_commit` is the first parent of the squash commit `fix_commit` on main.
  Test patch = `git diff base fix -- tests/` (every change under `tests/`, so the
  CMake registration stays consistent); fix patch = everything else. The test
  patch is applied to the base before the agent starts, and is checked to apply.
- The problem statement describes observable behavior of the unfixed code
  (what is called, what goes wrong, what is expected), as an issue a user could
  file. It must not name the fixed function's new internals, quote the fix, or
  mention the test case names as the answer.

## 6. Decision schema

One JSON object, nothing else.

- `accept`: all fields below. `reject`: `pr`, `verdict`, `reason` (the PR is not
  a good instance: no detecting test, no behavior change, compile-only oracle,
  pass-before test, out of profile). `hold`: `pr`, `verdict`, `reason` (a
  legitimate candidate that cannot be run now: needs an option not in CI, an
  unsupported target, or missing facts); it is not a soft reject, and is for
  infrastructure or evidence gaps only.

```
{ "pr": <int, the PR number>,
  "verdict": "accept" | "reject" | "hold",
  "reason": "<why>",
  "confidence": "high" | "medium" | "low",
  "change_type": "fix" | "feature",
  "suite": "unit" | "gtest",
  "target": "<CMake target from the inventory>",
  "cmake_options": [ ],                       # usually []; each must be in ci_cmake_options
  "fail_to_pass": [ ... ],                    # unit: the target's ctest name; gtest: Suite.Case
  "pass_to_pass": [ ... ],                    # may be []
  "problem_statement": "<observable behavior, no fix leakage>",
  "causal_chain": "<changed test input -> old production path -> expected failure -> fix -> pass>" }
```

Placeholder example. Every identifier, number and path is invented to show the
shape; it is not an execution result and not a PR you are given:

```
{ "pr": 99999, "verdict": "accept",
  "reason": "New assertion in the Widget gtest feeds an empty list that the old Widget::Parse() dereferences.",
  "confidence": "medium", "change_type": "fix",
  "suite": "gtest", "target": "ot-example-gtest", "cmake_options": [],
  "fail_to_pass": ["WidgetParse.EmptyListIsRejected"],
  "pass_to_pass": ["WidgetParse.SingleItem"],
  "problem_statement": "Parsing an empty widget list crashes instead of returning an error.",
  "causal_chain": "EmptyListIsRejected passes an empty list -> Widget::Parse() reads item 0 unconditionally -> crash/wrong status -> fix checks the length first -> status is kErrorParse and the test passes." }
```

The checker requires: `pr` matches; `verdict` one of the three; `reason`; for
accept, `confidence`, `change_type`, `suite`, a `target` that exists in the
inventory for that suite, `fail_to_pass` non-empty and made only of names in
that target's inventory (unit: exactly its ctest name), `pass_to_pass` names in
the same inventory and disjoint from F2P, `cmake_options` each listed in
`ci_cmake_options`, non-empty `problem_statement` and `causal_chain`, and a test
patch that applies to the base. Input or schema errors are sent back to you with
the reason; do not repair them by inventing names.

## 7. Oracle and inventory rules

- Every `fail_to_pass` entry needs its own justification in `causal_chain`:
  which changed test input makes it fail on the base code. Do not list a case
  because it is in the same file.
- Names come only from the record's inventory (and the source you read). The
  inventory is computed at the fix commit from the CMake files and the
  `TEST/TEST_F/TEST_P` macros of the sources each gtest executable compiles.
  A parametrized `TEST_P` appears as `Suite.Case`; it runs under a longer
  instantiated name, so avoid it as F2P unless nothing else detects the change.
- No baseline run exists at triage time; you must not claim that any case
  failed before. A diagnostic baseline is taken later; execution, not compile,
  is the signal.
- The tests as they will be run are those at the fix commit; nothing from other
  PRs or saved results is available to you.

## 8. Confidence and budget

- `high`: the changed test input visibly reaches the changed production lines
  and the old code's failure is certain from the source; names verified in the
  inventory; no option needs.
- `medium`: the chain is plausible but one step is inferred (e.g. a config macro
  default, an ordering, a crash that depends on memory layout).
- `low`: an accept you would not launch without review. Prefer `hold` or
  `reject` over low confidence when a missing fact would change the answer.
- Do not accept when the test is gated off in the supported configuration, when
  the detection is compile-only, or when you could not name why it fails before.
- Tool use is bounded (the prompt states the number of rounds). The record
  already has both diffs and the inventory; use tools for the surrounding
  source, config defaults (`src/core/config/`) and `etc/cmake/options.cmake`.
  When the rounds are spent, answer from what you have.
