> Historical EmbedEval validation report. Its commands refer to the original workspace; use this folder's README and the exported reproduce.py for portable commands.

# Validated synthetic pthread-key destructor instance

Method: **Option 2**. Status: validated and packaged as
`embedeval:zephyr-65697-synthetic-destructor`.

## Original instance and mechanism

Parent: `zephyr__zephyr-65697`, issue
https://github.com/zephyrproject-rtos/zephyr/issues/65696.
The original source is pinned to
`41b7c17ac4bd0198fc9bb3a0e55aa8d5e2fae96e`; the upstream production fix is
`330c820b9dad6a417431b98a20c97baa32d7dc43`.

The original bug frees slot zero of the pthread-key allocation bitmap regardless
of the supplied key. The fix computes the supplied object's pool offset and
releases that slot. The original regressions cover replacement of a deleted key
in a full pool and repeated creation/deletion. Existing controls associate keys
and data across one or multiple threads. Inspection included both versions of
lib/posix/key.c, the upstream fix, tests/posix/common/src/key.c, thread-exit
destructor processing in lib/posix/pthread.c, and the issue description.

Docker reproduced the original failure. The upstream fix passed all 51 tests.
Original metadata misspells `test_key_1toN_thread` as `test_key_1to1_thread`;
this bundle uses the actual source name without editing parent metadata.

## Why Option 1 was rejected

The evaluated Option 1 shortcut returns success when a deleted key has no data,
skipping its bitmap release. This differs from freeing the wrong slot and is
detected by the existing test. However, the test aborts before cleaning up its
full key pool, causing both later key controls to fail too. It gave 47 passes
and 4 failures, followed by 51 passes after restoration. This was not accepted
as clean localization. Evidence and the rejection report are in the sibling
`../zephyr-65697-empty-key/` directory. No rejected candidate was registered.

The original regression creates keys with NULL destructors, so it cannot detect
lost callback registration. A new focused test is necessary for the Option 2
mechanism below; existing tests are retained unchanged.

## Synthetic issue and oracle

Synthetic issue: thread-specific values are successfully associated, but their
registered destructor never runs when the worker exits, preventing the user's
resource cleanup. The production change omits `new_key->destructor = destructor`
in pthread_key_create. Object initialization leaves that field NULL, so thread
exit skips the callback. The original bitmap deletion fix remains present.

Before injection, the oracle was defined and communicated: a non-NULL callback
must run exactly once for a worker's non-NULL thread-specific value, receiving
that pointer. The new test creates a key, launches a worker using the original
stack and pthread setup conventions, associates a probe pointer, joins the
worker, destroys its attribute object, deletes the key, and then checks the
association result and callback count. The callback increments the probe passed
as its argument. Joining supplies synchronization. Cleanup precedes the final
assertion, so the intended failure does not consume shared key slots.

## Files and test changes

Inside the synthetic codebase:

- `lib/posix/key.c`: retains the upstream bitmap fix; removes callback
  registration. `synthetic-bug.diff` contains the exact injection and
  `repair.diff` its inverse.
- `tests/posix/common/src/key.c`: adds one 44-line block with a probe, two helpers,
  and `test_key_thread_exit_calls_destructor`. Existing 51 tests are unchanged.
  `synthetic-test.diff` contains the addition.

Host artifacts are confined to `outputs/synthetic/`: scripts, patches, source
snapshots, logs, results, and the standalone `instance/` Docker context. The
parent instance, shared Dockerfiles, harness, and registry were not edited.
The image inherits the original run_tests executable. A logging-only copy used
for validation prints the runner's captured QEMU output before its normal
temporary-file cleanup; test execution, timeout, and exit status are unchanged.

## Executed validation

Inside the existing Docker environment, every phase used:

```sh
cd /testbed
west build -b qemu_x86 tests/posix/common
bash /tmp/logged-run-tests 600
```

The logging wrapper runs the original `run_tests 600` logic. The current parent
runner counts half-second polling intervals as seconds, so its timeout label
overstates elapsed wall time. No phase timed out.

| Implementation and tests | Build | Pass | Fail | Runner exit |
|---|---|---:|---:|---:|
| Correct + synthetic test | PASS | 52 | 0 | 0 |
| Synthetic bug + identical tests | PASS | 51 | 1 | 1 |
| Restored correct + identical tests | PASS | 52 | 0 | 0 |
| Fresh packaged image | PASS | 51 | 1 | 1 |
| Packaged image after repair | PASS | 52 | 0 | 0 |

The only synthetic failure is:

```text
test_key_thread_exit_calls_destructor
(probe.destructor_calls not equal to 1)
expected one key destructor call, got 0
```

All 51 existing tests pass with the synthetic bug, including both original
regressions and both thread/key controls. Source restoration was byte-identical
to the correct baseline. Test-source and resolved Kconfig hashes are identical
across correct, synthetic, and restored phases. See `results.json`,
`packaged-results.json`, and the per-phase build/test logs for raw evidence.

## Reproduction

Run from the EmbedEval repository root. With the original image available:

```powershell
& .venv/pr13461/python/python.exe outputs/synthetic/zephyr-65697-destructor/validate.py
& .venv/pr13461/python/python.exe outputs/synthetic/zephyr-65697-destructor/package.py
docker build -t embedeval:zephyr-65697-synthetic-destructor outputs/synthetic/zephyr-65697-destructor/instance
& .venv/pr13461/python/python.exe outputs/synthetic/zephyr-65697-destructor/verify_packaged.py
```

Any working Python 3.10+ can replace the explicit interpreter. No model, API key,
or third-party host Python dependency is involved.

Run the buggy instance directly (expected exit 1):

```powershell
docker run --rm embedeval:zephyr-65697-synthetic-destructor bash -c "cd /testbed && west build -b qemu_x86 tests/posix/common && run_tests 600"
```

The parent image was built with the existing Zephyr base Dockerfile, SDK 0.16.8,
qemu_x86, and POSIX common suite. This Windows checkout's CRLF test patch failed
to apply in Linux; a separate LF-only copy of the original build context was
used, preserving all logical content and commands:

```powershell
docker build -f docker/bases/zephyr.Dockerfile -t embedeval-zephyr-base:latest docker/bases
docker build --build-arg BASE_COMMIT=41b7c17ac4bd0198fc9bb3a0e55aa8d5e2fae96e --build-arg PLATFORM=qemu_x86 --build-arg TEST_PATH=tests/posix/common -t embedeval:zephyr-65697 outputs/synthetic/zephyr-65697-empty-key/build-context
```

## Realism, similarity, and validity limits

Omitting callback registration during initialization or refactoring is a
plausible semantic mistake: code compiles, all old tests pass, and user resource
cleanup silently disappears. It introduces no special test-dependent branch,
syntax error, intentional crash, or arbitrary constant. The repair is one line
and the likely difficulty is low; its substance is the key/thread cleanup
contract rather than edit size. It should not be represented as a hard task.

Similarity is at the pthread-key resource-lifecycle level, in the same source
file and real implementation. The triggering API and state are distinct:
original key deletion corrupts allocation bookkeeping; this variation loses a
callback at creation, exposed at thread exit. It neither reverts nor reproduces
the original patch. It is broader than merely varying the deletion offset.

The new test is benchmark-authored, not upstream. It covers one thread with one
non-NULL value, not destructor iteration/reassociation, every NULL-value case,
concurrency stress, or physical hardware. Validation ran real Zephyr firmware
in its existing QEMU environment. Dependency downloads remain as unpinned as
the parent Dockerfiles; image IDs are preserved in packaged-results.json.

The bundle is deliberately identified as synthetic and remains standalone.
EmbedEval currently restricts registered IDs to numeric upstream-PR forms;
no fake PR number or registry change was introduced. The metadata documents
provenance, problem statement, protected tests, and expected test outcomes.
The solution patch is outside the Docker build context and is not installed
in the instance image.
