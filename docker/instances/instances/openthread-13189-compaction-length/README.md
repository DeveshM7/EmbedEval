# openthread-13189-compaction-length

This is one synthetic bug derived from [parent PR #13189](https://github.com/openthread/openthread/pull/13189);
it is not a new upstream PR. Method: **Option 1**.

## Problem statement

Clearing saved frames unexpectedly shortens a partially written frame that must be preserved. ClearSavedFrames must discard the saved region while retaining the pending payload and its reserved bytes. Preserve iterator invalidation, restarting, ordinary iteration, append, and discard behavior.

## Exact injected bug

The original upstream fix is applied first. This patch introduces the distinct
synthetic bug on top of the correct implementation:

```diff
--- a/src/lib/spinel/multi_frame_buffer.hpp
+++ b/src/lib/spinel/multi_frame_buffer.hpp
@@ -397,7 +397,7 @@
         if (len > 0)
         {
             memmove(mBuffer, mWriteFrameStart, static_cast<uint16_t>(mWritePointer - mWriteFrameStart));
-            mWritePointer -= len;
+            mWritePointer -= len + kHeaderSize;
             mWriteFrameStart -= len;
             mRemainingLength += len;
             mBufferCleared = true;
```

Production source: `src/lib/spinel/multi_frame_buffer.hpp`. Existing regression tests and controls are unchanged.
The expected failing tests are:

- `clear_saved_frames`

## Validated results

| State | Passing tests | Failing tests |
|---|---:|---:|
| Correct implementation | 7 | 0 |
| Synthetic bug | 6 | 1 |
| Restored implementation | 7 | 0 |

All relevant controls passed. A fresh packaged image reproduced the expected
failures, and applying only `repair.diff` made the entire suite pass.
See [the report](REPORT.md) and [saved evidence](evidence/) for details and limits.
These are historical results; newly executed results are stored separately.

## Reproduce from the exported repository root

```sh
python reproduce.py --instance openthread-13189-compaction-length --build --validate
```

Run the buggy image directly after building it (expected exit 1):

```sh
docker run --rm embedeval:openthread-13189-synthetic-compaction-length bash -c "cd /testbed && run_tests"
```

The repair patch is deliberately outside `build/`, so it is not copied into the
instance image. This public review repository exposes both the bug and repair;
an agent evaluation must provide only the intended instance environment.
