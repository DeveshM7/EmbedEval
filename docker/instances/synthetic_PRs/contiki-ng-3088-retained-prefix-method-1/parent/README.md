# Contiki-NG #3088

Source: https://github.com/contiki-ng/contiki-ng/pull/3088

## Revisions and upstream scope

The PR was **open and unmerged** when inspected on 2026-10-14. This instance
is a candidate pinned to its current PR head, not a merged commit or GitHub's
synthetic merge revision. If benchmark admission requires merged PRs, defer
admission until the final merge is checked.

- Benchmark base: `cf61c0dfa134665edff6315e13b9345c0b713d2f`.
- Pinned PR head: `ca16edcecbb6196685ce66794811446fac565228`.
- GitHub's reported current PR base: `48ed53bf19f907638152a027cba21d5740ae690e`.

The benchmark base precedes all three PR commits:

1. `c3b8a8718596c63a79666b529b3aeca316784d56`: receive-buffer retention.
2. `0bbb4ac075c15ed44ff90d99218e329df1eb1a53`: full retained-buffer guard.
3. `ca16edcecbb6196685ce66794811446fac565228`: simulation data validation.

Choosing the head's third parent isolates the complete change; using its
immediate parent would already include both production changes. The current
GitHub base tracks later main-branch work. Metadata's `fix_commit` and
`test_commit` both refer to the pinned PR head.

The complete base-to-head diff changes:

- `os/net/ipv6/tcp-socket.c`.
- `tests/07-simulation-base/code-ipv6/tcp-client/tcp-client.c`.
- `tests/07-simulation-base/code-ipv6/tcp-server/tcp-server.c`.

## Tests and scope

The upstream simulation is `tests/07-simulation-base/28-ipv6-tcp-sockets.csc`,
using `ipv6-tcp.js` and the TCP client/server sources. The PR changes the
sender's data pattern and adds receiver validation, but that receiver callback
always consumes all data. It does not exercise nonzero callback retention.
The complete Cooja simulation was not run on this host.

`test_patch.diff` preserves the exact upstream changes to both simulation
sources and adds six fixture files under the protected directory
`tests/08-native-runs/embedeval-tcp-retention`. It contains no production
source changes. `files_changed_by_fix` lists only `tcp-socket.c`, so the
standard validator's gold patch cannot alter any tests.

The component fixture compiles the complete real TCP socket source, Contiki
process dispatcher, and list library as separate translation units. Incoming
uIP events are delivered through `process_post_synch()` to the actual TCP
socket process; no static receive function is extracted or replaced. Only
uIP transport/event globals and boundary operations are scripted. The test
platform supplies compile-time configuration and clock declarations. This
is decoded socket-event/component validation, not a live TCP network or
simulation test.

Six benchmark regressions cover initial retained-state reset, retained suffix
contents, retention across events, retention during a larger-than-buffer
event, excessive callback retention, and an already full retained buffer.
The full-buffer case verifies bounded return, no extra callback, unchanged
retained bytes, and intact canaries; it does not promise that excess network
data is retried or that TCP backpressure is implemented by this PR.

Six component controls cover ordinary delivery, chunked fully consumed
streams, embedded zero/high-bit bytes, no input callback, send queue APIs,
and invalid registration. Six additional unchanged upstream data-structure
tests cover lists, stacks, queues, circular singly linked lists, doubly linked
lists, and circular doubly linked lists. Their full upstream source is
included without rewriting assertions; the wrapper invokes the existing
test functions and reads the upstream `passed` result field. Clock operations
are deterministic fixture boundaries.

| Test | Base | PR head |
| --- | --- | --- |
| registration_resets_retention | FAIL | PASS |
| suffix_compaction | FAIL | PASS |
| retain_across_events | FAIL | PASS |
| retain_within_large_event | FAIL | PASS |
| overretain_clamped | FAIL | PASS |
| full_retained_buffer_bounded | FAIL | PASS |
| ordinary_receive | PASS | PASS |
| large_stream | PASS | PASS |
| binary_payload | PASS | PASS |
| absent_callback | PASS | PASS |
| send_queue | PASS | PASS |
| invalid_registration | PASS | PASS |
| Six upstream data-structure tests | PASS | PASS |

## Build and runner

From the upstream repository root:

```sh
bash tests/08-native-runs/embedeval-tcp-retention/build.sh
python3 tests/08-native-runs/embedeval-tcp-retention/run.py all
```

The build uses the host C compiler in C11 mode, optimization, debugging
symbols, and section garbage collection. It builds `build/tcp-retention`
and `build/upstream-controls` from real upstream sources plus fixtures.
No board compiler, submodule, or external test library is needed. `CC` can
select another compatible host compiler.

The Dockerfile uses the existing `embedeval-contiki-ng-base:latest`, fetches
only the benchmark base, applies the test patch, installs the runner, and
prebuilds without executing the intentionally failing regressions. Only the
test patch and runner are copied into the image. Metadata, this report, local
validation tools and the gold diff stay outside it.

`run_tests.sh` rebuilds and supports `all` (default), `regression`, or
`control`, following the existing Contiki-NG runners. Individual component
tests have a five-second subprocess timeout so a loop cannot prevent later
tests from running. The upstream control executable has a thirty-second
timeout; the complete build/test invocation has a 120-second timeout.
Exit codes are 0 for pass, 1 for build/test failure, and 2 for timeout.
The existing `tests/` protection applies to all upstream tests and fixtures.

The problem statement describes observable buffering and bounded behavior;
it contains no upstream code, implementation algorithm, or exact edit.

## Validation — 2026-10-14

Validation used LLVM-MinGW 20260922 on Windows, with the same C11, optimization,
include directories, source lists and section garbage collection as the
Linux build script. No source compatibility changes were required. Both
base and PR-head component builds succeeded.

The final packaged test patch was checked and applied to a fresh base Git
checkout, then compiled and executed. Applying the exact upstream production
diff reproduced the pinned PR source. All eight patched test/fixture files'
SHA256 hashes stayed unchanged across revisions.

- **Base regression results:** six of six fail with assertion failures,
  not compilation or setup errors. Regression and combined groups exit 1.
- **Base control results:** all twelve pass; control group exits 0.
- **PR head results:** all eighteen tests pass; regression, control and
  combined groups each exit 0.
- **Partial-fix check:** the first retention commit passes five regressions
  but fails the full retained-buffer case. All controls pass. This confirms
  that the benchmark also requires the later full-buffer guard.
- **Metadata validation:** JSON loading through both build and harness
  accessors, naming, discovery, required fields, revision ancestry,
  full changed-file scope, test classifications, image/build arguments,
  and protected paths pass. EmbedEval has no separate schema validator.
- **Benchmark structure validation:** required artifacts, LF files,
  test-only patch paths, clean patch applicability, protected-file hashes,
  exact fixed-source comparison, Bash syntax, whitespace checks, base and
  instance build dry runs, and agent selection dry run pass.
- **Implementation exposure:** static task-description checks pass. The
  Docker image fetches only the base and copies no gold implementation.
- **Docker validation: UNVERIFIED.** Docker is absent. Actual construction
  and the standard `scripts/validate_instance.py` invocation stop when
  launching Docker with `FileNotFoundError` / WinError 2. Docker dry runs
  do not establish image construction or Linux container correctness.

No tests were removed, disabled, or weakened to make validation pass. Only
this new instance directory was added; unrelated existing files and changes
were preserved. Local validation tools and detailed results are under
ignored `.venv/pr3088/`.

On a Docker-enabled host, run from the EmbedEval root:

```sh
python scripts/build_bases.py --repo contiki-ng
python scripts/build_instances.py --instance contiki-ng__contiki-ng-3088
python scripts/validate_instance.py contiki-ng__contiki-ng-3088 --verbose
```
