> Historical EmbedEval validation report. Its commands refer to the original workspace; use this folder's README and the exported reproduce.py for portable commands.

# OpenThread #13494: late-order synthetic instance

Method: **Option 1**. Exactly **one synthetic production bug** was created.
Tests changed: **none**. Status: three-state validation and fresh packaged-image
verification passed.

## Parent and original mechanism

Parent: `openthread__openthread-13494`.
PR: https://github.com/openthread/openthread/pull/13494.
Pinned base: `255021a0b9675061ebd6e6bb3dcec53a68bdae45`.
Pinned upstream production fix: `c9d3215b0f1f685c78b7570b03ceedaa8d583dd9`.

The virtual-time polling budget expires just before the background reader enqueues a command's final lines, causing a false timeout.

The parent problem statement, regression tests, controls, relevant source, and
exact upstream production diff were inspected. Docker reproduced the original
expected regressions while all parent controls passed, then the exact fixed
upstream source passed the entire existing suite. Correct-source equality to
the pinned fixed Git blob was checked before injection.

## Synthetic mechanism, distinction, and pre-injection oracle

Grace-period lines are inserted at the start of the response instead of appended, reversing late output while retaining bounded waiting and completion detection.

This retains the original fix and changes a different semantic behavior rather
than reverting the original patch. The mechanism and oracle were communicated
before injection and recorded in `oracle.json`.

Oracle: Late responses must preserve transport arrival order, including payload rows before Done; ready, synchronous, error, callback, and timeout behavior remains unchanged.

Expected regression reason: The returned list is reversed: Done precedes diagnostic or ping response lines. There is no false timeout in the intended failures.

Synthetic problem statement: Late ping and networkdiagnostic responses return completion before their response data, with data lines in the wrong order. Preserve transport arrival order through command completion, including responses arriving after the virtual-time polling budget. Retain bounded timeouts and existing synchronous, ready-response, callback, and error behavior.

## Files and tests

Only `tools/otci/otci/command_handlers.py` changes in the synthetic codebase. Exact injection:

```diff
--- a/tools/otci/otci/command_handlers.py
+++ b/tools/otci/otci/command_handlers.py
@@ -225,7 +225,7 @@
                     except queue.Empty:
                         break
 
-                    output.append(line)
+                    output.insert(0, line)
 
                     if match_line(line, expect_line):
                         done = True
```

`repair.diff` reverses only the synthetic change. The fixed source snapshots,
upstream production diff, test logs, build logs, and per-test JSON results are
alongside this report. `tests/test_otci_pending_lines.py` and the original `run_tests` runner
are unchanged in every phase; their combined SHA256 is:

`ea86cd5b6555297f597896b7dd065d73fda3ff050a6224bc173205f6dc5c48ed`

No new test or replacement oracle was needed. The existing parent test patches
are benchmark-authored (the upstream PRs added no tests); they remain unchanged.
The original host instance files, metadata, shared Dockerfiles, harness, and
registry were not edited. Separate LF-normalized copies of original Windows
build contexts were used without changing logical file content.

## Results

| Phase | Build exit | Passing methods/cases | Failing methods/cases | Runner exit |
|---|---:|---:|---:|---:|
| original | 0 | 7 | 4 | 1 |
| correct | 0 | 11 | 0 | 0 |
| synthetic | 0 | 9 | 2 | 1 |
| restored | 0 | 11 | 0 | 0 |
| packaged | 0 | 9 | 2 | 1 |
| packaged-repaired | 0 | 11 | 0 | 0 |

Failing tests with the synthetic bug:

- `test_delayed_output_order`
- `test_delayed_diagnostic`

All other existing tests pass with the synthetic bug, including all original
parent controls. Restoring the correct implementation is byte-identical to the
correct baseline and returns every test to PASS. The fresh packaged image
reproduces exactly the same failures; applying only `repair.diff` makes it pass.
No timeout or compilation failure was accepted as the intended regression.

Observed failure: `['Done', 'DIAG_GET.rsp/ans from fd00::1']` is returned instead of `['DIAG_GET.rsp/ans from fd00::1', 'Done']`. The ping response is similarly reversed. Both failures are list-equality assertions, not timeouts.

The synthetic suite was repeated five additional times. Every repeat had the same two failing methods and nine passing methods, including all timeout controls. This checks the existing deterministic reader-thread scheduling boundary rather than assuming a single threaded run is stable.

## Commands and artifacts

Run from the EmbedEval root. Build all three parent images once with:

```powershell
docker build -f docker/bases/openthread.Dockerfile -t embedeval-openthread-base:latest docker/bases
& .venv/pr13461/python/python.exe outputs/synthetic/openthread-three/prepare_parents.py
```

Replay this case, build its synthetic image, and verify the packaged repair:

```powershell
& .venv/pr13461/python/python.exe outputs/synthetic/openthread-three/validate.py --only 13494
```

Any working Python 3.10+ may replace the explicit host interpreter; validation
requires Docker, Git/network access, and the existing container dependencies,
not an API key or agent libraries. The instance image retains its parent's
runtime, compiler, build command, test source, and runner.

Rebuild the already generated synthetic context directly:

```powershell
docker build -t embedeval:openthread-13494-synthetic-late-order outputs/synthetic/openthread-three/13494/instance
```

Run the buggy instance (expected exit 1):

```powershell
docker run --rm embedeval:openthread-13494-synthetic-late-order bash -c "cd /testbed && run_tests"
```

The validator also runs the metadata build command before every suite. Exact
commands for each build appear in the parent metadata and `instance/Dockerfile`.
`results.json` records immutable parent/synthetic image IDs and all outcomes.
No validation containers are left running.

## Realism and validity limits

Merging a deferred response tail as though it were older backlog is a plausible queue-integration mistake; it changes response semantics without changing scheduling.

These are small semantic changes with short repairs, likely low difficulty.
They are not syntax errors or test-specific special cases. Similarity is in
the same real subsystem and exercised workflow; the violated invariant differs
from the original bug mechanism. Only one bug is injected into each image.

Eleven unittest methods execute the real upstream command runner and background reader. A scripted transport controls the output-delivery boundary; this is not a firmware, real-device, or full virtual-time integration run. Seven upstream import-time filter assertions are included through the existing control method.

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
