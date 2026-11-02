> Historical EmbedEval validation report. Its commands refer to the original workspace; use this folder's README and the exported reproduce.py for portable commands.

# OpenThread #13189: compaction-length synthetic instance

Method: **Option 1**. Exactly **one synthetic production bug** was created.
Tests changed: **none**. Status: three-state validation and fresh packaged-image
verification passed.

## Parent and original mechanism

Parent: `openthread__openthread-13189`.
PR: https://github.com/openthread/openthread/pull/13189.
Pinned base: `4394c0944b9b96f449cd2556ea5b2c1be002ed93`.
Pinned upstream production fix: `ff121bfce3ce0fab05fa1188ca89cab83c9b3e0c`.

Clearing or compacting a saved-frame buffer leaves an active iterator pointing into replaced storage; continued iteration may treat stale bytes as a header.

The parent problem statement, regression tests, controls, relevant source, and
exact upstream production diff were inspected. Docker reproduced the original
expected regressions while all parent controls passed, then the exact fixed
upstream source passed the entire existing suite. Correct-source equality to
the pinned fixed Git blob was checked before injection.

## Synthetic mechanism, distinction, and pre-injection oracle

Compaction subtracts the internal header size in addition to the complete saved-region length, shortening the preserved pending frame by four bytes. Iterator invalidation remains correct.

This retains the original fix and changes a different semantic behavior rather
than reverting the original patch. The mechanism and oracle were communicated
before injection and recorded in `oracle.json`.

Oracle: ClearSavedFrames must preserve the complete 36-byte pending frame and its reserved bytes while invalidating the old iterator. Normal iteration, appending, and clear/restart behavior must remain unchanged.

Expected regression reason: GetLength() reports 32 instead of the required 36 after compaction; the existing preservation assertion fails before further iteration.

Synthetic problem statement: Clearing saved frames unexpectedly shortens a partially written frame that must be preserved. ClearSavedFrames must discard the saved region while retaining the pending payload and its reserved bytes. Preserve iterator invalidation, restarting, ordinary iteration, append, and discard behavior.

## Files and tests

Only `src/lib/spinel/multi_frame_buffer.hpp` changes in the synthetic codebase. Exact injection:

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

`repair.diff` reverses only the synthetic change. The fixed source snapshots,
upstream production diff, test logs, build logs, and per-test JSON results are
alongside this report. `tests/unit/test_multi_frame_buffer_recovery.cpp` and the original `run_tests` runner
are unchanged in every phase; their combined SHA256 is:

`849fbe9ad2a885a037258fb7072f46191eded2948a78177f68741fa69ffa6456`

No new test or replacement oracle was needed. The existing parent test patches
are benchmark-authored (the upstream PRs added no tests); they remain unchanged.
The original host instance files, metadata, shared Dockerfiles, harness, and
registry were not edited. Separate LF-normalized copies of original Windows
build contexts were used without changing logical file content.

## Results

| Phase | Build exit | Passing methods/cases | Failing methods/cases | Runner exit |
|---|---:|---:|---:|---:|
| original | 0 | 4 | 3 | 1 |
| correct | 0 | 7 | 0 | 0 |
| synthetic | 0 | 6 | 1 | 1 |
| restored | 0 | 7 | 0 | 0 |
| packaged | 0 | 6 | 1 | 1 |
| packaged-repaired | 0 | 7 | 0 | 0 |

Failing tests with the synthetic bug:

- `clear_saved_frames`

All other existing tests pass with the synthetic bug, including all original
parent controls. Restoring the correct implementation is byte-identical to the
correct baseline and returns every test to PASS. The fresh packaged image
reproduces exactly the same failures; applying only `repair.diff` makes it pass.
No timeout or compilation failure was accepted as the intended regression.

Observed failure: `FAILED TestClearSavedFramesDuringIteration:121 - VerifyOrQuit(buffer.GetLength() == 36)`. The source arithmetic subtracts four extra bytes, implying length 32 rather than 36; the unmodified test prints the failed 36-byte assertion, not the actual length.


## Commands and artifacts

Run from the EmbedEval root. Build all three parent images once with:

```powershell
docker build -f docker/bases/openthread.Dockerfile -t embedeval-openthread-base:latest docker/bases
& .venv/pr13461/python/python.exe outputs/synthetic/openthread-three/prepare_parents.py
```

Replay this case, build its synthetic image, and verify the packaged repair:

```powershell
& .venv/pr13461/python/python.exe outputs/synthetic/openthread-three/validate.py --only 13189
```

Any working Python 3.10+ may replace the explicit host interpreter; validation
requires Docker, Git/network access, and the existing container dependencies,
not an API key or agent libraries. The instance image retains its parent's
runtime, compiler, build command, test source, and runner.

Rebuild the already generated synthetic context directly:

```powershell
docker build -t embedeval:openthread-13189-synthetic-compaction-length outputs/synthetic/openthread-three/13189/instance
```

Run the buggy instance (expected exit 1):

```powershell
docker run --rm embedeval:openthread-13189-synthetic-compaction-length bash -c "cd /testbed && run_tests"
```

The validator also runs the metadata build command before every suite. Exact
commands for each build appear in the parent metadata and `instance/Dockerfile`.
`results.json` records immutable parent/synthetic image IDs and all outcomes.
No validation containers are left running.

## Realism and validity limits

Double-counting framing overhead is a plausible pointer-accounting error when converting between payload lengths and physical buffer spans.

These are small semantic changes with short repairs, likely low difficulty.
They are not syntax errors or test-specific special cases. Similarity is in
the same real subsystem and exercised workflow; the violated invariant differs
from the original bug mechanism. Only one bug is injected into each image.

Seven separate native executables invocations compile and execute the real header with upstream test helpers and GCC. Each case runs in its own process. This is not an end-to-end RCP/radio recovery run.

The preserved tests establish these focused behaviors, not complete subsystem
correctness, security, performance, or hardware compatibility. Parent dependency
installation remains as unpinned as its original Dockerfiles. Stored image IDs
identify the actual locally validated artifacts.

These are standalone, explicitly synthetic instances. Existing EmbedEval IDs
encode real numeric PRs, so no fake PR ID or harness registry change was made.
The repair patch, reports, and validation scripts stay outside the Docker build
context and are not installed in the image. Preparation patches are removed
from the running filesystem after application; this is not a forensic claim
about inaccessible underlying Docker layers.
