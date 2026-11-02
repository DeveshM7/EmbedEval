# nuttx-14152-negative-jump-value — Method 2

Method: **Option 2**. Parent: [https://github.com/apache/nuttx/pull/14152](https://github.com/apache/nuttx/pull/14152).

## Original bug mechanism

longjmp with zero returns zero rather than the required one.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `5a38c8bfe3cbf906010a906b4aff9f104789a63f` to `89d6abf3dfcf0bec86ed10ec9494995b6f3b41b4`.

## Synthetic bug mechanism and distinctness

A signed-positive branch increments negative nonzero values instead of preserving them.

The original upstream fix is retained. A signed-positive branch increments negative nonzero values instead of preserving them. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

longjmp(buf,-7) must resume setjmp with -7, while the original 123 and zero cases remain passing.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `libs/libc/machine/sim/arch_setjmp_x86_64.S`.

Tests: `apps/testing/ostest/setjmp.c`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 2 | 0 | 0 |
| synthetic | 1 | 1 | 1 |
| restored | 2 | 0 | 0 |

Fail-to-pass tests: `negative_jump_value`.
Pass-to-pass tests: `full_ostest`.

Exact reason for regression failure: A signed-positive branch increments negative nonzero values instead of preserving them. longjmp(buf,-7) must resume setjmp with -7, while the original 123 and zero cases remain passing. The negative request -7 is incremented to -6 by the fall-through path.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

Confusing nonzero with positive is a plausible signedness mistake in assembly control flow.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The full original NuttX kernel/apps checkout, original toolchain base, sim:nsh configuration, original apps regression patch, and complete ostest suite are retained. Assertions are enabled. CONFIG_TESTING_OSTEST_WAITRESULT is enabled so completion cannot be reported before the worker finishes. The runner waits for user_main completion and treats added regression failures as failures even when the overall application returns zero. Full ostest completion is one aggregate control result, not a claim of per-assertion test enumeration. This validates Linux x86-64 simulation, not physical hardware or other architectures.



## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance nuttx-14152-negative-jump-value --build --validate
```

Build command: `make -j4`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/libs/libc/machine/sim/arch_setjmp_x86_64.S
+++ b/libs/libc/machine/sim/arch_setjmp_x86_64.S
@@ -131,7 +131,7 @@
 
 	movl	%esi,%eax
 	testl	%eax,%eax
-	jnz	1f
+	jg	1f
 	incl	%eax
 
 	/* Restore registers */
```
