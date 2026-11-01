# openthread-13189-reclaimed-capacity — Method 2

Method: **Option 2**. Parent: [https://github.com/openthread/openthread/pull/13189](https://github.com/openthread/openthread/pull/13189).

## Original bug mechanism

Clearing or compacting storage leaves an old saved-frame iterator active.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `4394c0944b9b96f449cd2556ea5b2c1be002ed93` to `ff121bfce3ce0fab05fa1188ca89cab83c9b3e0c`.

## Synthetic bug mechanism and distinctness

Compaction moves data and pointers but fails to return reclaimed bytes to the writable-capacity counter.

The original upstream fix is retained. Compaction moves data and pointers but fails to return reclaimed bytes to the writable-capacity counter. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

After dropping saved frames, CanWrite must include the reclaimed space and writes into it must succeed without altering the pending payload.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `src/lib/spinel/multi_frame_buffer.hpp`.

Tests: `tests/unit/test_multi_frame_buffer_recovery.cpp`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 8 | 0 | 0 |
| synthetic | 7 | 1 | 1 |
| restored | 8 | 0 | 0 |

Fail-to-pass tests: `reclaimed_capacity`.
Pass-to-pass tests: `append_during_iteration`, `clear_empty`, `clear_longer`, `clear_same_boundary`, `clear_saved_frames`, `clear_shorter`, `normal_iteration`.

Exact reason for regression failure: Compaction moves data and pointers but fails to return reclaimed bytes to the writable-capacity counter. After dropping saved frames, CanWrite must include the reclaimed space and writes into it must succeed without altering the pending payload. CanWrite(112) is false because the cached remaining length stays at 72 after reclaiming 40 bytes.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

A storage-compaction change can update pointers while overlooking cached capacity; iterator invalidation remains correct.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The original parent image, pinned runtime/compiler, production source, original regression suite, and existing controls are retained. This is a focused host test, not a firmware or radio integration test.



## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance openthread-13189-reclaimed-capacity --build --validate
```

Build command: `mkdir -p build && c++ -std=c++11 -Wall -Wextra -Werror -O0 -g -Iinclude -Isrc -Isrc/include -Itests/unit tests/unit/test_multi_frame_buffer_recovery.cpp -o build/ot-test-multi-frame-buffer-recovery`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/src/lib/spinel/multi_frame_buffer.hpp
+++ b/src/lib/spinel/multi_frame_buffer.hpp
@@ -399,7 +399,7 @@
             memmove(mBuffer, mWriteFrameStart, static_cast<uint16_t>(mWritePointer - mWriteFrameStart));
             mWritePointer -= len;
             mWriteFrameStart -= len;
-            mRemainingLength += len;
+            // Remaining capacity is unchanged by compaction.
             mBufferCleared = true;
         }
     }
```
