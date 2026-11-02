# nuttx-8885-signal-sign-bit

Method: **Option 2**. Parent: [https://github.com/apache/nuttx/pull/8885](https://github.com/apache/nuttx/pull/8885).

## Original bug mechanism

A scalar signal set cannot represent the expanded 64-signal range.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `0eae218b49a46ef88e14f1cb763a6ca74fe342ab` to `717bb04cb7c6f1efb179d40a464e43e7cd13c7d8`.

## Synthetic bug mechanism and distinctness

A masked membership value is converted to signed and compared with zero; the high bit of each word becomes negative and is falsely reported absent.

The original upstream fix is retained. A masked membership value is converted to signed and compared with zero; the high bit of each word becomes negative and is falsely reported absent. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

Signals 31 and 63, added with sigaddset, must be reported present. Adjacent bits, deletion, and invalid-number behavior must remain correct.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `libs/libc/signal/sig_ismember.c`.

Tests: `apps/testing/ostest/ostest.h`, `apps/testing/ostest/ostest_main.c`, `apps/testing/ostest/sighelper.c`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 11 | 0 | 0 |
| synthetic | 9 | 2 | 1 |
| restored | 11 | 0 | 0 |

Fail-to-pass tests: `signal_boundary_31`, `signal_boundary_63`.
Pass-to-pass tests: `full_ostest`, `signal_boundary_30`, `signal_boundary_32`, `signal_boundary_62`, `signal_delete_30`, `signal_delete_31`, `signal_delete_32`, `signal_delete_62`, `signal_delete_63`.

Exact reason for regression failure: A masked membership value is converted to signed and compared with zero; the high bit of each word becomes negative and is falsely reported absent. Signals 31 and 63, added with sigaddset, must be reported present. Adjacent bits, deletion, and invalid-number behavior must remain correct. The selected bit is 0x80000000; its signed interpretation is negative, so membership returns 0 instead of 1.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

Signed interpretation of bit masks is a common boundary error; storage remains the upstream two-word representation.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The full original NuttX kernel/apps checkout, original toolchain base, sim:nsh configuration, original apps regression patch, and complete ostest suite are retained. Assertions are enabled. CONFIG_TESTING_OSTEST_WAITRESULT is enabled so completion cannot be reported before the worker finishes. The runner waits for user_main completion and treats added regression failures as failures even when the overall application returns zero. Full ostest completion is one aggregate control result, not a claim of per-assertion test enumeration. This validates Linux x86-64 simulation, not physical hardware or other architectures.

 Original metadata omitted two signal ABI migration files; include their exact pinned upstream changes to build the correct implementation. Unrelated serial/architecture changes between the metadata revisions are excluded.

## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance nuttx-8885-signal-sign-bit --build --validate
```

Build command: `make -j4`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/libs/libc/signal/sig_ismember.c
+++ b/libs/libc/signal/sig_ismember.c
@@ -66,7 +66,7 @@
     {
       /* Check if the signal is in the set */
 
-      return ((set->_elem[_SIGSET_NDX(signo)] & _SIGNO2SET(signo)) != 0);
+      return ((int32_t)(set->_elem[_SIGSET_NDX(signo)] & _SIGNO2SET(signo)) > 0);
     }
 }
```
