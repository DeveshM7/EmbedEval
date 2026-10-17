# NuttX Phase 2: all ostest apps PRs

- Run: `nuttx-apps-all` on Scholar at
  `/scratch/scholar/zhan5136/embedeval/runs/nuttx-apps-all/`.
- Input: all 189 merged `nuttx-apps` PRs changing `testing/ostest/` in the
  enriched manifest. Each was sent to the frozen Phase 1 winner `68660ee811`
  (normal repeats 12/13, 13/13, 13/13; prompt SHA-256
  `69e201aa67261cb242971012e4aec34fa83bf5cff104473d95ac6299873e6148`).
- Pipeline implementation commit: `5f6c19f70cf65bc240a704faf7fd196396992534`.
  The triage prompt and sampling settings remained frozen; later code commits
  corrected validation and result classification.
- Pipeline jobs: `477278`, `477299`, `477302`, `477316`, `477318`, `477337`,
  `477339`, `477344`, `477354`, `477360`, `477363`. Jobs `477312` and `477314`
  were canceled before running with the correct ARM64 Python; `477330` was
  canceled while pending to resize its retry batch. These were our own jobs.

The final machine report (2026-10-17 09:08 local) is `report.md` and
`report.json` in the run folder. Per-PR verdicts, statuses and pair validation
reports are retained in its `triage/`, `status/` and `instances/` directories.

## Funnel

All 189 PRs were attempted by the winner. It returned usable accept/reject
verdicts for 185; three repeatedly reached the 30-minute cap and one returned
an error. The 62 accepted PRs proposed 63 high/medium pairs (61 high, two
medium); one PR proposed two pairs. Every accepted PR has a terminal
validation state.

| Outcome | PRs |
|---|---:|
| Triage reject | 123 |
| Triage cap | 3 |
| Triage error | 1 |
| VALID | 10 |
| INVALID | 9 |
| Validation failed or inconclusive | 21 |
| `needs_x86` | 10 |
| `arm64_env_failure` | 8 |
| `qemu_pending` | 4 |

The counts sum to 189. A `validation: failed` PR is not an INVALID instance.

## Validated pairs

Each listed report has `valid: true`, a failing pre-fix build or run, and
post-fix build and `ostest` run exit 0. The report path is
`instances/apps-<apps PR>-kernel-<kernel PR>/validation.json` under the run.

| Apps PR | Kernel PR | Pre-fix evidence |
|---:|---:|---|
| 1857 | 9923 | `ostest` run rc 1 |
| 2327 | 11889 | `ostest` run rc 1; libc test crash, exact failing assertion not isolated |
| 2329 | 11898 | `ostest` run rc 1 |
| 2353 | 12106 | `ostest` run rc 1 |
| 2458 | 12802 | build rc 2: `nuttx/event.h` missing |
| 3177 | 16993 | build rc 2: undefined `nxevent_getmask` |
| 3559 | 19118 | `ostest` run rc 1: multi-user test failure |
| 3701 | 19678 | `ostest` run rc 1 |
| 3733 | 19867 | build rc 2: undefined `atomic_add` |
| 3790 | 20112 | `ostest` run rc 1 |

## Limits and adjudications

- Kernels before 2022-02-02 without ARM64 sim are `needs_x86`; kernels affected
  by the ARM64 vfork environment failure are `arm64_env_failure`. Neither is
  counted INVALID, and no x86 validation path was built. The current
  `needs_x86` apps PRs are #170, #237, #241, #293, #546, #649, #651, #652,
  #912 and #955. The `arm64_env_failure` apps PRs are #1358, #1526, #1665,
  #1682, #1711, #1741, #1813 and #1962.
- Four pairs need QEMU boards other than the integrated `qemu-armv8a` path;
  they remain `qemu_pending`: apps #1141 (`qemu-rv:nsh`), #2420
  (`qemu-rv64ilp32:nsh`), #2457 (`qemu-rv-virt:knsh64`) and #3067
  (`qemu-armv7a:smp`). Generator/build failures and timeouts remain validation
  failures or inconclusive, not INVALID.
- Apps #2137 → kernel #10934 timed out in this pipeline without follow-up
  #10970, which the winner omitted. A separate run with #10970 was VALID, but it
  is not a VALID result for this pipeline pair.
- Apps #3553 proposed two high-confidence pairs: kernel #19162 still failed
  after its fix, while #19165 failed to link after its fix. The PR-level
  outcome is inconclusive, and both pair reports are retained.
- Apps #3759 → kernel #19982 was corrected from an apparent VALID to INVALID:
  the changed test did not run before the fix. Apps #3677 → kernel #19578 was
  likewise INVALID because the pre-fix compile test built successfully. Their
  settled ledger entries reflect the corrected outcomes.
- Apps #3280, #3347 and #3728 remained capped after unchanged-winner retries.
  Apps #646 ended with an output-rule error; one retained reply proposed
  kernel #3272 with follow-ups, but it was rejected by the parser and is an
  unvalidated lead, not an accepted pair. Prior statuses and traces are in
  the run's `retry-*` directories.
- Phase 1 apps #1665 → kernel #8832 was independently VALID on x86 with
  either HPWORK-only or LPWORK, preserving the one-of config gold rule. Its
  Phase 2 ARM64 result is `arm64_env_failure`.

No instances were promoted and no branch was pushed.

## Viability of the 22 blocked pairs (branch `nuttx-viability`)

Verdicts were seeded from `nuttx-apps-all` (`prepare.py --verdicts-from`), not
re-triaged. Runs on Scholar: `nuttx-viability-x86` (18 old-era sim pairs,
`cpu` partition, x86_64), `-x86b` and `-x86c` (the 4 QEMU pairs, x86 nodes,
plus apps #241 again). Apps #237 and #241 are not triage-capped: both have an
accepted pair (#997, #1007), so they were validated.

Platform decisions: group 1 and group 2 run on x86 (instance metadata
`host_arch: x86_64`; build, validate and evaluate refuse other hosts). For
group 2 no ARM64 backport was attempted: the broken vfork code
(`sim_vfork_arm64.S`) is the 2022 implementation that the 2023-07 rework
(c33d1c9c97, a repo-wide rename plus a new fork path) replaced, so a small
fix is not available.

| Apps PR | Kernel PR | Outcome | Platform |
|---:|---:|---|---|
| 170 | 766 | INVALID: changed test did not run before the fix | sim:nsh x86_64 |
| 237 | 997 | INVALID: tests passed before the fix | sim:nsh x86_64 |
| 241 | 1007 | INVALID: changed test did not run before the fix | sim:nsh x86_64 |
| 293 | 1236 | VALID | sim:nsh x86_64 |
| 546 | 2631 | INVALID: tests passed before the fix | sim:nsh x86_64 |
| 649 | 3299 | VALID | sim:nsh x86_64 |
| 651 | 3299 | VALID | sim:nsh x86_64 |
| 652 | 3308 | VALID | sim:nsh x86_64 |
| 912 | 4941 | VALID | sim:nsh x86_64 |
| 955 | 5120 | VALID | sim:nsh x86_64 |
| 1358 | 5070 | still blocked: pre-fix run hangs in priority_inheritance (timeout rc=2); the run predates the merged hang-as-failure rule, not re-run | sim:nsh x86_64 |
| 1526 | 8397 | still blocked: ostest times out with the fix | sim:nsh x86_64 |
| 1665 | 8832 | VALID | sim:nsh x86_64 |
| 1682 | 8885 | VALID | sim:nsh x86_64 |
| 1711 | 7464 (+9039) | INVALID: changed test did not run before the fix | sim:nsh x86_64 |
| 1741 | 9203 | INVALID: tests passed before the fix | sim:nsh x86_64 |
| 1813 | 9632 | INVALID: tests passed before the fix | sim:nsh x86_64 |
| 1962 | 9755 | still blocked: apps base does not build on the kernel base (nshlib needs a newer kernel API) | sim:nsh x86_64 |
| 1141 | 6080 | INVALID on rv-virt:nsh (rv32): pre-fix link error as expected, but FPU#1 takes an illegal-instruction fault after the fix. riscv_initialstate.c of that era never sets mstatus.FS, so no rv-virt FPU test can pass. Another FPU board (e.g. sabre-6quad under QEMU) untried | qemu rv32, x86 node |
| 2420 | 12535 (+12475) | still blocked: the toolchain (Ruyi rv64ilp32 GCC) is x86 only; the follow-up patch of #12475 does not apply even with prerequisites ordered first (not diagnosed) | qemu rv64, x86 node |
| 2457 | 12820 | still blocked: knsh64 (BUILD_KERNEL, export/import, hostfs) builds but run_tests times out; not debugged | qemu rv64 knsh64, x86 node |
| 3067 | 16301 | VALID | qemu-armv7a:smp, x86 node |

Counts: VALID 9 (293, 649, 651, 652, 912, 955, 1665, 1682 on sim; 3067 on QEMU),
INVALID 8 (170, 237, 241, 546, 1711, 1741, 1813, 1141), blocked 5 (1358, 1526,
1962, 2420, 2457). None of the QEMU pairs ran on an ARM64 node:
the `qemu-misc` image is only built and exercised on x86 so far.

Changes: x86 platform option (`host_arch`, per-architecture image names),
`prepare.py --verdicts-from`, optional `slurm.gres`, `scripts/nuttx_platforms.py`
(board table), the `qemu-misc` image (RISC-V old/new, ilp32, armv7-a), an
Apptainer sim runner that feeds nsh one command at a time (2020 kernels drop
early input), NDEBUG check for old kernels, banners from guard changes and
banner-less compile cases, prerequisite-first ordering of fix patches.
Open decision for the user: x86-only instances in the benchmark.
