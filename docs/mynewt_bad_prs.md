# Apache Mynewt PR triage: six benchmark rejects

These are valid upstream engineering changes, but they should be rejected from
the strict EmbedEval candidate pool. Here, **bad PR** means unsuitable as a
benchmark instance: it does not mean that the upstream change itself is bad.

A Mynewt benchmark candidate must be a merged PR tied to an originating GitHub
issue, change an executable regression testcase in the PR itself, have
separable production and test patches, demonstrate fail-before/pass-after
behavior, and run deterministically with `hw/bsp/native` and `compiler/sim`.

None of the six PRs below has been packaged as an EmbedEval instance or run
through the current `scripts/validate_instance.py`. Their rejection confidence
comes from PR metadata and diff inspection, not from a validator trace.

## [#3708 — Free a sensor listener when registration fails](https://github.com/apache/mynewt-core/pull/3708)

**Evidence**

- The PR is merged and closes [issue #3703](https://github.com/apache/mynewt-core/issues/3703).
- Its only changed file is `hw/sensor/src/sensor.c`; the production fix adds a
  single `free()` call on an error path.
- The PR changes no selftest package, testcase, assertion, or other executable
  regression test.
- The PR description explicitly says no reproduction harness was used and the
  sensor failure path was not exercised at runtime.

**Why it should be rejected**

The issue provenance is strong, but there is no test patch that can be applied
to the base revision to evaluate an agent-generated fix. Code inspection alone
cannot supply the protected fail-before/pass-after oracle required by
EmbedEval.

**Confidence:** High  
**Expected rejection point:** Hard filter — no executable testcase changed.  
**Validator-proven:** No — no instance or current-validator run exists.

## [#3707 — Fix AT45DB settings leak and global baud-rate corruption](https://github.com/apache/mynewt-core/pull/3707)

**Evidence**

- The PR is merged and closes [issue #3701](https://github.com/apache/mynewt-core/issues/3701).
- Its only changed file is `hw/drivers/flash/at45db/src/at45db.c`.
- The PR changes no Mynewt selftest or executable testcase.
- Its verification used a standalone reproduction because the author did not
  have the physical AT45DB flash hardware or a full Mynewt build environment.

**Why it should be rejected**

The standalone reproduction is not part of the merged repository and cannot
be separated into an EmbedEval test patch. The affected initialization path is
also tied to a physical SPI flash driver, with no checked-in native-simulator
oracle for the leak or shared-setting corruption.

**Confidence:** High  
**Expected rejection point:** Hard filter — no executable testcase changed.  
**Validator-proven:** No — no instance or current-validator run exists.

## [#2511 — Replace `strtok` with `strtok_r`](https://github.com/apache/mynewt-core/pull/2511)

**Evidence**

- The PR is merged and closes [issue #2510](https://github.com/apache/mynewt-core/issues/2510).
- It modifies `apps/pwm_test/src/pwm_shell.c`,
  `hw/drivers/sensors/bno055/src/bno055_shell.c`, and
  `hw/sensor/src/sensor_oic.c`.
- The `apps/pwm_test` path is a demonstration/test application, not a unit-test
  package containing a named testcase and assertions.
- No changed file detects the reported thread-safety problem by exercising
  concurrent tokenization before and after the fix.

**Why it should be rejected**

The test-like application name is misleading for automated discovery. The PR
contains only production/application changes and provides no deterministic
regression oracle that distinguishes `strtok()` from `strtok_r()`.

**Confidence:** High  
**Expected rejection point:** Hard filter — no executable regression testcase.  
**Validator-proven:** No — no instance or current-validator run exists.

## [#821 — Add PWM blinking and fix `os_dev_close`](https://github.com/apache/mynewt-core/pull/821)

**Evidence**

- The PR is merged and closes [issue #820](https://github.com/apache/mynewt-core/issues/820).
- It changes `apps/pwm_test/src/main.c` and the physical nRF52 PWM driver at
  `hw/drivers/pwm/pwm_nrf52/src/pwm_nrf52.c`.
- The application change adds visible LED blinking; it does not add a named
  automated testcase or assertions for clean device shutdown.
- Verifying the observable behavior requires nRF52 PWM hardware and an attached
  LED rather than `hw/bsp/native` with `compiler/sim`.

**Why it should be rejected**

This is a hardware demonstration, not an emulator-compatible regression test.
An agent patch could not be graded deterministically from the merged test
changes.

**Confidence:** High  
**Expected rejection point:** Model triage — physical hardware and no automated oracle.  
**Validator-proven:** No — no instance or current-validator run exists.

## [#2157 — Remove `LOG_VERSION` 2 support](https://github.com/apache/mynewt-core/pull/2157)

**Evidence**

- The PR is merged and closes [issue #2120](https://github.com/apache/mynewt-core/issues/2120).
- It removes version-2 logging implementation and headers across the log
  subsystem.
- Paths under `sys/log/full/selftest/` are changed, but every such change only
  removes a `LOG_VERSION` setting from `syscfg.yml`.
- No selftest C testcase or assertion is added or modified to detect the
  production-code removal.

**Why it should be rejected**

A path-based search could mistake the selftest configuration changes for a
regression test. They only keep existing packages buildable after removing an
obsolete option; they do not create a test patch that fails on the base
revision because of missing behavior.

**Confidence:** High  
**Expected rejection point:** Model triage — selftest paths changed, but no detecting testcase.  
**Validator-proven:** No — no instance or current-validator run exists.

## [#3509 — Fix mixed-pool `os_mbuf_dup()` overflow](https://github.com/apache/mynewt-core/pull/3509)

**Evidence**

- The PR is merged and changes production code in `kernel/os/src/os_mbuf.c`.
- It adds `kernel/os/selftest/src/testcases/os_mbuf_test_dup_pool.c` and
  registers the new cases in `kernel/os/selftest/src/mbuf_test.c`.
- The tests check pool selection, segment lengths, payload preservation, and
  guard bytes, making this otherwise a strong native-simulation candidate.
- GitHub reports no closing issue for the PR. Its description references
  [apache/mynewt-nimble PR #2118](https://github.com/apache/mynewt-nimble/pull/2118),
  which is another pull request rather than an originating issue report.

**Why it should be rejected**

This is the important provenance boundary case: strong code and strong tests
do not satisfy the dataset rule when there is no linked originating GitHub
issue from which to derive the problem statement. It should remain a
conditional lead only if that rule is later relaxed.

**Confidence:** High  
**Expected rejection point:** Provenance filter — no linked originating issue.  
**Validator-proven:** No — no current-validator run exists; it may be mechanically valid despite failing the provenance rule.
