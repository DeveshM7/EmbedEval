# Contiki-NG #3214

Source: https://github.com/contiki-ng/contiki-ng/pull/3214

## Revisions and status

The PR was **open, draft, and unmerged** when inspected on 2026-10-14.
This instance is a candidate pinned to its current head. If admission requires
merged PRs, wait for the final revision and revalidate it.

- Benchmark base: `7f844118d0235db405cae54f69cc1d4e7f0bd788`.
- Pinned PR head: `d806f5dc295615789d1b42e8ddd4e3571f5789a0`.

The base is the head's third first-parent ancestor (`head~3`) and matches
GitHub's reported PR base.
It precedes all three PR commits:

1. `0259c2a419380a69469430a7c0bc98a800749ae0`: simulation application.
2. `e0bf0f221e2cd3abc489dacba56cdb9ed26598f4`: regression simulation.
3. `d806f5dc295615789d1b42e8ddd4e3571f5789a0`: production fix.

Using the complete pre-PR base makes the upstream test patch reproducible.
The synthetic merge revision is not used. Both `fix_commit` and `test_commit`
refer to the pinned PR head.

The complete PR diff changes five files:

- `os/services/orchestra/orchestra-rule-unicast-per-neighbor-rpl-ns.c`.
- `tests/07-simulation-base/29-rpl-tsch-orchestra-time-source-change-ns.csc`.
- `tests/07-simulation-base/code-orchestra-txrx/Makefile`.
- `tests/07-simulation-base/code-orchestra-txrx/node.c`.
- `tests/07-simulation-base/code-orchestra-txrx/project-conf.h`.

## Upstream test and component benchmark

The PR's Cooja simulation changes the root's position after the network has
settled, causing a time-source/parent change, and expects ten application
packets to reach the destination through the relay. All four upstream test
files are preserved in `test_patch.diff` with normalized line endings;
no assertions, timeout,
topology, or simulation script is changed.

This host has no available Java/GNU Make/Cooja installation, and the shared
Contiki-NG base image does not provide a Cooja toolchain. The full upstream
simulation was **not executed**, and its end-to-end result is **unverified**.
The component runner explicitly does not claim to run that simulation.

The benchmark adds six files under
`tests/08-native-runs/embedeval-orchestra`: `main.c`, `contiki-conf.h`,
`rtimer-arch.h`, `upstream-controls.c`, `build.sh`, and `run.py`.
It compiles these real upstream components separately:

- Orchestra callback dispatch and the non-storing unicast rule.
- TSCH schedule allocation, link insertion/removal, and lookup.
- Packet buffer attributes, link addresses, lists, and memory blocks.

Queue operations, locks, logging, the unused root-rule predicate, and
sniffer registration are scripted boundaries. The queue fixture records
cleanup requests; it does not implement packet delivery. No Orchestra
selection function or schedule storage implementation is replaced or
extracted. RPL non-storing headers and the receiver-based unicast rule are
selected; other Orchestra rules are outside this focused component scope.

Tests use the real public Orchestra callbacks to add neighbors and change
time source, then inspect actual TSCH links. Packet-selection checks use the
real packet buffer and dispatcher and require that the selected cell exists
and is transmit-capable. Ten repeated selections exercise the former neighbor
after a switch; these are scheduling checks, not ten radio deliveries.

| Regression | Base | PR head |
| --- | --- | --- |
| switch_preserves_neighbor_link | FAIL | PASS |
| loss_preserves_neighbor_link | FAIL | PASS |
| queued_frames_not_discarded | FAIL | PASS |
| non_neighbor_not_scheduled | FAIL | PASS |

The first two cases preserve a neighbor after changing or losing time source.
The third rejects erroneous queue cleanup for a still-present neighbor.
The fourth checks that a time-source notification does not invent a transmit
cell without a neighbor-add event. The latter is an isolated callback-order
case, not a claim about how often that ordering occurs in a live RPL network.

Eight component controls cover neighbor addition/removal and queue cleanup,
an unchanged time source, local receive scheduling, unicast data selection,
broadcast/non-data filtering, and an unrelated neighbor. Six additional
unchanged upstream data-structure tests cover lists, stacks, queues, circular
singly linked lists, doubly linked lists, and circular doubly linked lists.
The wrapper includes their complete source without rewriting assertions and
reads the upstream `passed` results. All fourteen controls pass on both source
revisions.

## Build, runner, and conventions

From the upstream repository root:

```sh
bash tests/08-native-runs/embedeval-orchestra/build.sh
python3 tests/08-native-runs/embedeval-orchestra/run.py all
```

The host C11 build uses O2, debugging symbols, section garbage collection,
and real upstream headers. It builds `build/orchestra` and
`build/upstream-controls`. No submodule, external library, cross compiler,
radio hardware, or emulator is needed for the component tests.

`run_tests.sh` rebuilds and supports `all` (default), `regression`, and
`control`, following existing Contiki-NG runners. Runtime cases have
five-second limits, upstream controls have thirty seconds, and the complete
build/test invocation has 180 seconds. It continues after individual failures.
Exit codes are 0 pass, 1 build/test failure, and 2 timeout. All eighteen
selected test IDs are classified in metadata.

The Dockerfile uses the existing `embedeval-contiki-ng-base:latest`, fetches
only the base commit, applies the test-only patch, installs the runner, and
prebuilds without executing expected failing regressions. Metadata and this
report stay outside the image. The upstream Cooja files and all added fixtures
are under the existing protected `tests/` path. The gold patch is restricted
to the sole production file and cannot change tests.

The task statement describes lost scheduling and preserved neighbor behavior
without supplying the upstream code, exact edit, or callback implementation.

## Validation — 2026-10-14

Native validation used LLVM-MinGW 20260922 on Windows, with the same C11,
optimization, include paths, source lists and link options as the Linux build
script. Both ordinary builds succeeded without source compatibility edits.

The final packaged patch was checked and applied to a fresh archived base
in an isolated Git repository, then compiled and run. Applying the exact
production diff reproduced the pinned PR source. All ten test/fixture files'
SHA256 hashes remained unchanged across revisions.

- **Base regressions:** all four fail with actual missing-cell, erroneous
  queue-cleanup, or inappropriate-cell assertions. Regression and combined
  groups exit 1; these are not build/setup failures.
- **Base controls:** all fourteen pass; control group exits 0.
- **Pinned PR head:** all eighteen selected tests pass. Regression, control,
  and combined groups exit 0; individual component cases pass as well.
- **Metadata validation:** build and harness JSON loading, naming, discovery,
  required fields, three-commit base relation, production-file scope,
  disjoint classifications, image/build arguments, and protected paths pass.
  EmbedEval has no separate metadata-schema validator.
- **Structure validation:** required artifacts, LF files, test-only patch
  paths, clean patch applicability, fixed-source identity, protected-file
  hashes, Bash syntax, whitespace checks, build-base/instance dry runs,
  and agent selection dry run pass.
- **Task-description exposure:** static checks pass; no upstream
  implementation or exact edit is included in the problem statement.
- **Docker validation: UNVERIFIED.** Docker is absent. Actual instance
  construction and `scripts/validate_instance.py` were attempted but cannot
  launch Docker (`FileNotFoundError` / WinError 2). Docker dry runs do not
  prove image construction or Linux container correctness.
- **Upstream Cooja simulation: UNVERIFIED.** Its files are preserved unchanged,
  but no full simulation or end-to-end radio/network delivery was run.

No tests were removed, disabled, or weakened to make validation pass.
Only this new instance directory was added. Existing unrelated files and
working-tree changes were preserved. Detailed local results and tools are
under ignored `.venv/pr3214/`.

On a Docker-enabled host, run from the EmbedEval root:

```sh
python scripts/build_bases.py --repo contiki-ng
python scripts/build_instances.py --instance contiki-ng__contiki-ng-3214
python scripts/validate_instance.py contiki-ng__contiki-ng-3214 --verbose
```

These commands validate the component benchmark. Running the preserved Cooja
simulation additionally requires the upstream simulation toolchain.
