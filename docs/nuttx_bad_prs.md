# Apache NuttX: seven benchmark rejection examples

These are legitimate upstream changes. “Bad” means they should not be accepted
as **kernel-fix EmbedEval instances** on the evidence currently available. The
list spans both repositories and is meant to test later triage instructions.
The first six findings come from merged PR descriptions and changed-file
diffs. Two of them (#1792, #3401) were later packaged and run through
`scripts/validate_instance.py`; both ended ambiguous because the changed test
never ran under `sim:nsh`, as noted in their entries. The seventh (#2137) comes
from a validator run. A candidate with a new, reproducible old-code failure and
fixed-code pass can be reconsidered.

## [nuttx #20091 — serialize `clearenv()`](https://github.com/apache/nuttx/pull/20091)

**Evidence:** The merged kernel PR changes only
`sched/environ/env_clearenv.c`, adding the task-group mutex around
`env_release()`. Its testing section says the existing `sim:ostest` suite ran.
A targeted search for `20091`, `nuttx/pull/20091`, and `env_clearenv` in
`nuttx-apps` PRs found no companion regression-test PR.

**Reject point:** A passing pre-existing `ostest` run is not a changed test
patch. There is no test to apply to the old kernel that has been shown to fail
for this race. The targeted search is evidence of an unresolved pairing, not
proof that no test exists anywhere.

**Expected rejection point:** Discovery, unless a linked apps regression test
is found. **Confidence:** High for the absent changed test in this PR;
conditional for the broader ecosystem search.

## [nuttx-apps #1792 — follow the pthread cleanup Kconfig change](https://github.com/apache/nuttx-apps/pull/1792)

**Evidence:** The merged apps PR says it must merge with
[kernel PR #9527](https://github.com/apache/nuttx/pull/9527). Its changed
`ostest` Makefile and C sources replace guards on `CONFIG_PTHREAD_CLEANUP`
with guards on `CONFIG_PTHREAD_CLEANUP_STACKSIZE`. The kernel PR removes the
old Kconfig symbol and its dependency on the stack-size setting. The apps PR
adds no new cleanup assertion; it changes when existing cleanup tests compile
and run.

**Reject point:** This is a paired cross-repository compatibility update. A
new apps test patch that changes only feature guards does not by itself show
that old kernel behavior fails and fixed behavior passes. The old revision
already had the stack-size symbol under the old cleanup option. Treating
every paired apps change as a regression test would mistake test enablement
for a detecting testcase.

**Expected rejection point:** Triage of the paired PRs. **Confidence:** High
for the changed hunks.

**Validator run (2026-10-11):** packaged against kernel #9527 for `sim:nsh`.
On the old kernel `CONFIG_PTHREAD_CLEANUP_STACKSIZE` does not exist, so the new
guards compile the cleanup test out; ostest finishes without printing
`user_main: pthread_cleanup test` and `run_tests` exits 3 (ambiguous). There is
no old-code failure to compare against.

## [nuttx-apps #3676 — gate the perf test in non-flat builds](https://github.com/apache/nuttx-apps/pull/3676)

**Evidence:** The merged PR changes the `ostest` Makefile, CMakeLists,
`ostest.h`, and `ostest_main.c` to compile and call `perf_gettime_test()` only
under `CONFIG_BUILD_FLAT`. The PR describes an undefined-symbol link failure
in protected/kernel builds and says the affected test is skipped after this
change.

**Reject point:** This repairs test selection in `nuttx-apps`; it does not add
a testcase that detects a missing kernel behavior. QEMU execution is possible
for the cited target, but that does not turn a skipped test into a regression
oracle.

**Expected rejection point:** Triage, because the changed test is disabled on
the failing target. **Confidence:** High.

## [nuttx-apps #3767 — increase work-queue test stack](https://github.com/apache/nuttx-apps/pull/3767)

**Evidence:** The merged PR changes only `testing/ostest/wqueue.c`. Its
functional change replaces a literal `2048` worker stack size with the
existing `STACKSIZE` constant; the PR says 2048 is too small on intel64.
No kernel PR or new assertion is identified in the PR.

**Reject point:** This is test-harness maintenance to let an existing test
run on another target. It does not isolate a kernel production fix. An
intel64/QEMU target is not itself a reason to reject it.

**Expected rejection point:** Triage, because the change is test maintenance
without a kernel fix. **Confidence:** High.

## [nuttx-apps #3401 — remove board-loop calibration from tests](https://github.com/apache/nuttx-apps/pull/3401)

**Evidence:** The merged PR closes
[nuttx-apps issue #3345](https://github.com/apache/nuttx-apps/issues/3345)
and changes only `testing/ostest/sporadic.c`, `sporadic2.c`, and
`testing/sched/smp/smp_main.c`. It replaces test-local delay loops that depend
on `CONFIG_BOARDLOOPSPERMSEC` with clock-based loops. The PR cites
[nuttx #17011](https://github.com/apache/nuttx/pull/17011) as the change that
exposed the compile error and demonstrates a QEMU build.

**Reject point:** The repair is in the apps test sources themselves. Pairing
the changed test with a kernel revision would not leave a kernel fix for the
agent to implement. This is a two-repo directionality case: a link to a
kernel PR is not enough to make the kernel PR the benchmark fix.

**Expected rejection point:** Triage of fix direction and patch separation.
**Confidence:** High.

**Validator run (2026-10-11):** packaged with only the two `testing/ostest/`
files against kernel #17011 for `sim:nsh`. `CONFIG_SCHED_SPORADIC` is not set
there, so neither sporadic test is compiled or run; ostest exits 0 and
`run_tests` exits 3 (ambiguous). This says nothing about the fix either way.

## [nuttx-apps #2137 — SMP function call test](https://github.com/apache/nuttx-apps/pull/2137)

**Evidence:** The apps PR adds `testing/ostest/smp_call.c`, gated by the new
`CONFIG_SMP_CALL`, calling `nxsched_smp_call_single()` and `nxsched_smp_call()`.
[Kernel PR #10934](https://github.com/apache/nuttx/pull/10934) adds those APIs
in `sched/sched/sched_smp.c`, which relies on the per-architecture hook
`up_send_smp_call()`. The PR implements that hook only for arm64
(`arch/arm64/src/common/arm64_gicv3.c`).

**Validator run (2026-10-11):** packaged for `sim:nsh` with `CONFIG_SMP`, its
dependent options, and `CONFIG_SMP_CALL` forced on. On the old kernel the link
fails with `undefined reference to 'nxsched_smp_call_single'`, the expected
failure. After applying the kernel fix, the link still fails with
`undefined reference to 'up_send_smp_call'` because the simulator has no
implementation. The fixed kernel cannot pass on `sim:nsh`.

**Reject point:** No passing fixed state on the native simulator. It also adds
a feature rather than fixing a bug, which is a separate policy question.

**Expected rejection point:** Triage of the runnable target, or validation.
**Confidence:** High for `sim:nsh`; an arm64 QEMU target could revisit it.

## [nuttx-apps #3785 — sensor watchdog coverage](https://github.com/apache/nuttx-apps/pull/3785)

**Evidence:** The merged apps PR adds `testing/drivers/drivertest` sensor
coverage and explicitly pairs
[nuttx issue #20145](https://github.com/apache/nuttx/issues/20145) with
[kernel fix #20146](https://github.com/apache/nuttx/pull/20146). The added
test prints “Sensor device ... not present, skipping test” when no sensor is
available, and also skips when the device lacks interval support. The PR says
the test does not deterministically reproduce the use-after-free.

**Reject point:** This has the expected issue, kernel PR, and apps test PR,
but the available native-sim path can skip the case, and a passing run would
not establish old-code failure. It needs a sensor fixture or emulator and a
reproducible fail-before/pass-after trace before acceptance. QEMU remains a
possible way to revisit it if that fixture can be provided.

**Expected rejection point:** Reproduction and validation, unless a fixture
produces a deterministic failure. **Confidence:** Medium, because another
target or fixture may make the regression observable.

## What these examples test

The current hard filter keeps all six apps PRs above; it leaves #3676 and
#3767 unresolved rather than guessing a kernel match. Enrichment resolves
#3785's scoped #20145 as an issue and retains #20146 as the kernel PR. The
kernel-only #20091 has no changed apps testcase to enter this apps-first
candidate stream.

The hard filter should preserve apps PRs that touch `testing/` so later
triage can examine them; it must not mistake the path alone for an executable
regression test. Enrichment must distinguish a scoped kernel issue from a
kernel PR, preserve explicit cross-repository pairing evidence, and retain
unresolved leads. Triage must identify which repository contains the fix,
whether the changed test detects it, what target actually runs the test, and
whether old code plus the new test fails for the intended reason. The final
decision still belongs to live instance validation.
