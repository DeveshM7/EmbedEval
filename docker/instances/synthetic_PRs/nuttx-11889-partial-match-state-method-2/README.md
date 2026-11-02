# nuttx-11889-partial-match-state — Method 2

Method: **Option 2**. Parent: [https://github.com/apache/nuttx/pull/11889](https://github.com/apache/nuttx/pull/11889).

## Original bug mechanism

The search misses the final candidate and lacks the empty-needle special case.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `5ac401d941da22109be4de2ec28a77b86b790144` to `47026978bf7d1f3db6f86a98a8e6ba73024f9489`.

## Synthetic bug mechanism and distinctness

Partial-match state is initialized once instead of once per candidate. A failed prefix can make a later nonmatching region appear to match.

The original upstream fix is retained. Partial-match state is initialized once instead of once per candidate. A failed prefix can make a later nonmatching region appear to match. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

Searching four bytes abXc for two bytes ac must return NULL. All original memmem assertions must still pass.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `libs/libc/string/lib_memmem.c`.

Tests: `apps/testing/ostest/libc_memmem.c`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 2 | 0 | 0 |
| synthetic | 1 | 1 | 1 |
| restored | 2 | 0 | 0 |

Fail-to-pass tests: `partial_prefix_mismatch`.
Pass-to-pass tests: `full_ostest`.

Exact reason for regression failure: Partial-match state is initialized once instead of once per candidate. A failed prefix can make a later nonmatching region appear to match. Searching four bytes abXc for two bytes ac must return NULL. All original memmem assertions must still pass. Observed semantic result: a non-NULL pointer to Xc is returned despite no ac substring.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

A loop-invariant-code-motion optimization incorrectly treats per-candidate state as invariant.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The full original NuttX kernel/apps checkout, original toolchain base, sim:nsh configuration, original apps regression patch, and complete ostest suite are retained. Assertions are enabled. CONFIG_TESTING_OSTEST_WAITRESULT is enabled so completion cannot be reported before the worker finishes. The runner waits for user_main completion and treats added regression failures as failures even when the overall application returns zero. Full ostest completion is one aggregate control result, not a claim of per-assertion test enumeration. This validates Linux x86-64 simulation, not physical hardware or other architectures.



## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance nuttx-11889-partial-match-state --build --validate
```

Build command: `make -j4`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/libs/libc/string/lib_memmem.c
+++ b/libs/libc/string/lib_memmem.c
@@ -61,9 +61,9 @@
       return NULL;
     }
 
+  y = 0;
   for (i = 0; i <= haystacklen - needlelen; i++)
     {
-      y = 0;
       while (h[i + y] == n[y])
         {
           if (++y == needlelen)
```
