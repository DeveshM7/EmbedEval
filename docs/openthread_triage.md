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
- Bug fixes and small features are both fine (`change_type`), provided the test
  drives an interface that already exists at the base commit. Linked issues are
  not required.
- Most candidates are not tasks. In a source-read sample of the candidate pool
  (2024-11 to 2026-10) roughly one in five had a usable runtime oracle; the rest
  were option-gated, compile-only, or did not detect anything (see section 4).
  Rejecting or holding is the normal outcome; accepting needs a traced chain.

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
  `ot_unit_ncp_test(name)` (`ot-test-ncp-<name>`), inside `if(OT_FTD)` since
  2026-10-23; a few
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
  over time; the record's `profile_cmake_options` list what the profile already
  sets, and its `ci_cmake_options` come from the base commit's own
  `.github/workflows/unit.yml`.
- **Pool and eras** (226 candidates, merged 2024-11 to 2026-10; 4-13 per month
  in 2025, 6-20 per month in 2026). Unit-only 89%, gtest-only 6%, both
  5%. Layout changes that matter for selection:
  - mbedtls is vendored under `third_party/mbedtls/repo` for bases before
    2025-11-12 and a git submodule (with a nested submodule) afterwards.
  - GoogleTest: bases before 2025-03-25 download it with `FetchContent` at
    configure time; later bases use the system package (`find_package`). The
    profile enables gtest on every build, so unit-only candidates of the older
    era configure it too.
  - `tests/unit/CMakeLists.txt` and `tests/gtest/CMakeLists.txt` gained
    `if(OT_FTD)`/`if(OT_RCP)` guards on 2026-10-23; before that the gtest file
    builds two executables unguarded (FTD and RCP radio-spinel), and more later.
  - The multi-PAN CI option was `OT_MULTIPAN_TEST` before 2025-09-05 and is
    `OT_MULTIPAN_RCP` after; multi-PAN targets need `OT_FTD=OFF` and never
    belong to the FTD profile.
  - `OT_WAKE_INITIATOR`/`OT_WAKE_LISTENER` exist only from 2026-08; earlier the
    profile's flags for them have no effect. The base `unit.yml` lists them only
    for the most recent bases.
  - The registration macros, `script/cmake-build` common option list, C++ 11 and
    cmake 3.10 minimum are the same in all eras.
- **Shared test helpers.** `tests/unit/test_platform.*`, `tests/unit/test_util.*`
  and `tests/gtest/fake_platform.*` are linked into many executables and belong to
  no single one. About one candidate in seven changes one. A helper edit detects
  nothing by itself; accept only if a named executable's own changed assertions
  fail without the production fix.
- **Local test guards.** Large tests wrap their body in `#if` blocks, often
  through a locally defined `ENABLE_..._TEST` macro (a conjunction of several
  `OPENTHREAD_CONFIG_*` macros) or a derived macro such as `OT_CONFIG_*`. The
  record's "Preprocessor guards" lists only direct `OPENTHREAD_CONFIG_*` macros;
  read the top of the test source with `read_file` for the others.
- The tools `read_file`, `list_dir`, `grep` read this repository; `ref` `base`
  is the base commit and `merge` (the default) is the fix commit.

## 3. Supported execution profile

Verified (one control run on the pinned revision above, both suites): Ubuntu
24.04 container, gcc/g++ 13.3, cmake 3.28, ninja, GoogleTest 1.14 from system
packages, aarch64 host, no network during configure, build or test (network is
used only to build the image and fetch the source and its recursive submodules).

Configure is upstream's own `script/cmake-build simulation`. The profile's
options are everything that script passes for `simulation` at the base commit
(its built-in list, which grows slightly over time and includes integer-valued
options) followed by `-DOT_BUILD_GTEST=ON -DOT_BORDER_ROUTING=ON
-DOT_BORDER_ROUTING_DHCP6_PD=ON -DOT_WAKE_INITIATOR=ON -DOT_WAKE_LISTENER=ON`
(later wins for the same option). The record's "Options already set in the
execution profile" lists the result; it is the authority, and an option it does
not list is not set by the profile. Your `cmake_options` are added
to this list (replacing an entry that sets the same option), and
`-DOT_COMPILE_WARNING_AS_ERROR=OFF` is always applied last, so compiler
warnings are not the oracle. Only the one target being run is built. FTD, MTD and
RCP are left at their defaults (all on per `etc/cmake/options.cmake`); OpenThread's simulation platform is used,
not a hardware or `external` platform.

**The toranj profile.** Under the profile above many unit tests compile their body
out (they print "... not enabled" and pass). Upstream runs those in its
`toranj-unittest` CI job (`./tests/toranj/build.sh all`, then `ninja test`), and
the second execution profile, `toranj`, mirrors it. Its options are the `cmake`
command of the `all | cmake)` case of `tests/toranj/build.sh` at the base commit
(the script's defaults substituted for coverage, log level, platform key ref
and CSL debug), then `-DOT_COMPILE_WARNING_AS_ERROR=OFF` last; nothing else is
added and `cmake_options` do not apply. Configure is plain
`cmake -GNinja -S <source> -B <build>`. Its `OT_PROJECT_CONFIG`,
`tests/toranj/openthread-core-toranj-config-simulation.h` (which includes the
common toranj header), is included before the simulation platform's config
header, which is included before the defaults in `src/`. It builds
`tests/unit` but no GoogleTest (`OT_BUILD_GTEST` is not set), so only unit
executables run under it. A base without a usable `all | cmake)` case has no
toranj profile.

Which profile a record uses is mechanical: simulation, unless the body of every
changed unit test source selected under simulation is disabled there and
enabled under toranj (and no gtest executable is selected); then toranj. The
record's "Execution profile" section says which and why, and its options and
guard resolution are those of the selected profile. A verdict uses the record's
profile unless it sets `profile`; it may set `"profile": "toranj"` only when the
record shows toranj available, the suite is `unit`, `cmake_options` is `[]` and
the target is built under toranj, and `"profile": "simulation"` to keep the
simulation one. Request toranj when the detecting unit test's body is disabled
under simulation but enabled under toranj.

Run: a gtest target's binary is executed and results are read per case
(`Suite.Case`); a unit test is executed through ctest by its `ot-test-<name>`
name, and passes or fails as a whole executable (`FAILED` plus a non-zero exit at
the first failed `VerifyOrQuit`/`SuccessOrQuit`, otherwise "All tests passed").
On the pinned revision the FTD gtest target (13 cases) and one unit executable
built and passed in about 30-35 s each.

Not verified, so treat as source-predicted: any base commit other than the
pinned revision (older eras were never built here: vendored mbedtls, the network
`FetchContent` GoogleTest before 2025-03-25, compilers newer than the code's own
CI); gtest targets other than the FTD one, including the radio-spinel RCP target
(it configures by default but was not built); and every detection claim, since no
baseline exists at triage time. For bases before 2025-03-25 say so in `reason` and
use at most `medium` confidence. Targets that need `OT_FTD=OFF` or
`OT_MULTIPAN_RCP` (see `requires_options`), or whose needed option is neither in
the profile list nor in `ci_cmake_options`, are not part of the profile: `hold`.

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
   compiled out and the executable passes before and after. Resolve the guarded
   macro's value in section 12's order (profile list, then the simulation
   platform's config header, then the default in the config header). The record's
   "Guards resolved under the execution profile" section already did this
   statically: it gives each guard's value, the macros behind it and where each
   value came from, plus whether the test body is enabled. Trust those values over
   your own reading of headers. A body that is not enabled cannot fail, so reject
   unless a CI option (listed there) would enable it; unknown means check
   yourself. A needed
   option may be added in `cmake_options` only if the profile does not already
   provide the value and it appears verbatim in `ci_cmake_options`
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

9. **Patterns that look like detectors but are not** (each seen repeatedly in
   the candidate pool; reject unless the trace shows otherwise):
   - *Constants or timing only.* A changed literal is a detector only if the old
     production code computes a different result for it (an expected value in a
     table, a boundary input). Literals that merely feed a parser, mirror a new
     default that the test never reads back, or extra `AdvanceTime(...)` calls
     that let new behaviour settle are accommodations: they pass before the fix.
   - *Test adapted to new behaviour but tolerated by the old code.* E.g. an
     added enable call or reordered setup that the old code also accepts; a
     test that opens its own socket/fake where the fix changes the server's.
   - *Rename or refactor with the old name still valid* (type alias, forwarding
     wrapper): the edited test compiles and passes on the base.
   - *Zero-filled or default data.* If the new input or expected value is all
     zero, empty, or equals what the old code returns anyway, the assertion holds
     before the fix.
   - *Unreached path.* The fake platform or test double never reaches the changed
     production line, or an earlier unchanged assertion in the same unit
     executable already decides the result.
   - *Helper-only or plumbing diffs*: stubs added to a shared test platform,
     CMake/flag plumbing, bulk mechanical edits (formatting, header guards,
     renames across many files) pass the hard filters but change no behaviour.
10. **A symbol absent from the base is a build failure, not a runtime
    oracle.** If any added test line references a function, overload, type,
    enum value or constant that the production diff introduces (including a new
    overload of an existing name or a renamed member), the base cannot build the
    target. Reject as "compile-only oracle" even if the new API looks behavioural;
    do not rely on the symbol's name being present elsewhere at the base (the same
    name often exists on another class). Roughly a quarter of candidates are of
    this kind, especially features and public API additions.
11. **What a good runtime oracle looks like.** A fix whose test (a) calls an
    interface that exists at the base, (b) feeds an input the old code mishandles
    (malformed or boundary encoding, repeated call, ordering, a table row whose
    expected output changed) and (c) asserts a concrete value or countable side
    effect. Unit executables stop at the first failed check, so the new
    assertion must be reachable: nothing earlier in that executable may already
    fail or crash on the base for unrelated reasons. A new gtest `TEST` that
    drives an existing method through the fake platform and counts effects is the
    other common good shape.
12. **Gating by the profile.** Before relying on a test or a production path,
    confirm it is compiled in: no `requires_options`, and direct guards and any
    local `ENABLE_...` macro conjunctions must all hold. Resolve each macro's
    value in this order, and stop at the first source that sets it:
    (a) the record's "Options already set in the execution profile" list, mapped
    through `etc/cmake/options.cmake` (`ot_option`/`ot_int_option` give the
    `OPENTHREAD_*` macro an `OT_*` option sets; a default-off macro whose option
    the profile list sets is on);
    (b) the profile's project config header, if it has one (toranj:
    `tests/toranj/openthread-core-toranj-config-simulation.h` and the header it
    includes), which takes precedence, then the simulation platform's core config header
    (`examples/platforms/simulation/openthread-core-simulation-config.h`, or its
    location at that base; check with the tools) and any project or core config
    headers it includes;
    (c) the default in the config header that defines the macro
    (`src/core/config/`, `src/lib/spinel/openthread-spinel-config.h`, ...).
    Never conclude "off" from a default alone, and never from an option's absence
    from `ci_cmake_options`, before checking (a) and (b). Only when the profile
    does not provide the needed value but an option in `ci_cmake_options` does,
    request it in `cmake_options`. If neither provides it, `hold` (the test is
    legitimate but not runnable under policy); if the test also would pass before
    the fix, `reject`.

## 5. Patch separation and problem statement

- `base_commit` is the first parent of the squash commit `fix_commit` on main.
  Test patch = `git diff base fix -- tests/` (every change under `tests/`, so the
  CMake registration stays consistent); fix patch = everything else. The test
  patch is applied to the base before the agent starts, and is checked to apply.
- `problem_statement` is what the agent sees, in place of the PR text. Write it
  from the PR title, body, commit message and the detecting assertion, as an
  issue a user could file: the **observable symptom** of the unfixed code, the
  **conditions that trigger it** (inputs, configuration, sequence), and the
  **behavior required**. Concrete values from the test inputs are fine.
- **Never the fix.** Do not name a function, class, file or line to edit; do not
  include code, identifiers or constants taken from the production diff; do not
  describe the implementation, the data structure or the algorithm used; do not
  say which branch or check is missing. Do not paraphrase the PR title when it
  describes the implementation (titles like "use X instead of Y", "derive A from
  B", "add a check in F", "reject ... in G()") -- restate the behavior instead.
  Do not mention test case names as the answer.
- **Feature tasks.** State the new required behavior in terms of the interface the
  test already calls. If the test would need a new function, type or constant
  name, the task is compile-only and is rejected, so the statement never has to
  introduce one.
- The agent separately sees the names of the failing tests and the list of files
  the fix changes. The statement must not add location hints beyond that: no
  subsystem file names, no function names, no hints about which layer is wrong.
- Keep it short (a few sentences). Describe behavior of the unfixed code, not
  what the benchmark will check.

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
  "profile": "simulation" | "toranj",         # optional; default is the record's profile (toranj: unit suite, cmake_options [])
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
`ci_cmake_options`, `profile` (when set) simulation or an available toranj for a unit target, non-empty `problem_statement` and `causal_chain`, and a test
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
- A unit executable passes or fails as a whole, so it can detect the fix with
  any one new assertion; attribution comes from where its output stops. Check
  that the PR's new assertion, not an older one, is what fails on the base.
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
- Cap at `medium` when the base is older than 2025-03-25 (network GoogleTest),
  when the gtest target is not the FTD one, when a unit executable has several
  possible detecting hunks, or when the only evidence of a crash is memory layout.
- Do not accept when the test is gated off in the supported configuration, when
  the detection is compile-only, or when you could not name why it fails before.
- Tool use is bounded (the prompt states the number of rounds). The record
  already has both diffs and the inventory; use tools for the surrounding
  source, config defaults (`src/core/config/`) and `etc/cmake/options.cmake`.
  When the rounds are spent, answer from what you have.
