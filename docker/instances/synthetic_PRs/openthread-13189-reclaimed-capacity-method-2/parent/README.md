# OpenThread #13189

Source: https://github.com/openthread/openthread/pull/13189

The merged fix is `ff121bfce3ce0fab05fa1188ca89cab83c9b3e0c`.
Its sole parent, `4394c0944b9b96f449cd2556ea5b2c1be002ed93`, is the
benchmark base. GitHub's PR base SHA is
`9cc2cb3e96b8282fdc31725bcb397eceb7f4a53a`; using the merged commit's parent
isolates the final fix without including intervening main-branch changes.
The PR head is `ea917d214cefc6630e48a169d944daf7a93f178b`.

The merged PR changes only `src/lib/spinel/multi_frame_buffer.hpp` and adds
no tests. Therefore `test_commit` is omitted: `test_patch.diff` contains
benchmark-authored tests, not tests attributed to an upstream commit.
The existing `tests/unit/test_hdlc.cpp::TestSpinelMultiFrameBuffer` checks
ordinary iteration, clearing, skip lengths, and buffer operations, but does
not resume an old iteration after clearing and saving replacement frames.

The new test compiles the real header directly, with upstream test helpers
and no substituted implementation. It models the buffer operations in
`SpinelDriver::ProcessFrameQueue` and `RadioSpinel::RecoverFromRcpFailure`.
It tests stale cursors before, at, and beyond the replacement region's end,
compaction with a pending frame, and restarting iteration. Control cases
check ordinary iteration and appending/discarding without clearing.
This is a deterministic buffer-level reproducer, not an end-to-end RCP test.

## Validation

Local verification on Windows with MSVC succeeded at both compilation steps:

| Case | Base | Upstream fix |
| --- | --- | --- |
| normal_iteration | PASS | PASS |
| append_during_iteration | PASS | PASS |
| clear_empty | PASS | PASS |
| clear_shorter | FAIL | PASS |
| clear_same_boundary | PASS | PASS |
| clear_longer | FAIL | PASS |
| clear_saved_frames | FAIL | PASS |

The exact upstream source diff was applied without changing the tests.
The Windows-only verification used `/Zc:preprocessor` and `/D__SDCC` to select
OpenThread's empty toolchain attribute macros, since upstream does not recognize
MSVC. The container uses native GCC with no compatibility defines.

EmbedEval's `build_bases.py` and `build_instances.py` dry runs passed. Actual
builds and `validate_instance.py` were attempted but stopped with
`FileNotFoundError` because Docker is unavailable. **The Docker build and
container-based fail-then-pass validation remain unverified.** On a host with
Python and a running Docker engine, run from the EmbedEval repository root:

```sh
python scripts/build_bases.py --repo openthread
python scripts/build_instances.py --instance openthread__openthread-13189
python scripts/validate_instance.py openthread__openthread-13189 --verbose
```

The image checks out only the base commit and applies only the test patch.
It does not copy metadata, this README, or the upstream fix into the image.
The problem statement describes the symptom and expected behavior without
describing the upstream implementation. `tests/` is protected by the harness.
As with other instances, fix provenance in metadata is used by the validator;
the harness sends only the problem statement and test names to the agent.
