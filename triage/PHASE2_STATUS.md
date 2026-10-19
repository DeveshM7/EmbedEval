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

## Combined results after the retries (2026-10-18)

The investigation of the failed and INVALID pairs (validation fixes on
`feature/nuttx-pr-filtering`: config overrides re-applied after the fix, the
pre-fix build deciding compile vs runtime, the GitHub 3000-file cap, config
fallbacks, a pre-fix hang counted as a failure, later follow-up kernel PRs
with build wiring first and a retry that really regenerates the pair) and the
viability work above. Re-runs on Scholar are archived per batch under
`runs/nuttx-apps-all/retry-*/` (each with a README naming the code snapshot).

**25 VALID pairs, 24 distinct kernel PRs** (apps #649 and #651 share #3299).

| Apps PR | Kernel PR | Platform | Note |
|---:|---:|---|---|
| 293 | 1236 | sim:nsh, x86_64 only | |
| 649 | 3299 | sim:nsh, x86_64 only | same kernel PR as #651 |
| 651 | 3299 | sim:nsh, x86_64 only | |
| 652 | 3308 | sim:nsh, x86_64 only | |
| 912 | 4941 | sim:nsh, x86_64 only | |
| 955 | 5120 | sim:nsh, x86_64 only | |
| 1665 | 8832 | sim:nsh, x86_64 only | |
| 1682 | 8885 | sim:nsh, x86_64 only | |
| 1857 | 9923 | sim:nsh | |
| 2327 | 11889 | sim:nsh | |
| 2329 | 11898 | sim:nsh | |
| 2353 | 12106 | sim:nsh | |
| 2458 | 12802 | sim:nsh | |
| 2725 | 14152 | sim:nsh, x86_64 only | the bug is x86-only (passes before the fix on ARM64); metadata needs `host_arch: x86_64` at promotion |
| 2822 | 14656 | sim:nsh | VALID once the sim runner fixes reached ARM64 |
| 3067 | 16301 | qemu-armv7a:smp, x86 node | |
| 3177 | 16993 | sim:nsh | |
| 3380 | 18174 | sim:nsh | pre-fix hang counted as the failure |
| 3443 | 18660 | sim:nsh | fix PR has >3000 files: whole diff |
| 3559 | 19118 | sim:nsh | |
| 3701 | 19678 | sim:nsh | |
| 3714 | 19767 | sim:nsh | runtime, not compile_error as triaged |
| 3733 | 19867 | sim:nsh | |
| 3759 | 19982 | sim:nsh | config fallback: copy `config_overrides_used` into the metadata before promotion, or grading fails |
| 3790 | 20112 | sim:nsh | |

Not VALID on ARM64 (`nuttx-apps-all`):

| Outcome | Apps PR (kernel PR) |
|---|---|
| INVALID: tests pass before the fix | 2636 (13283), 3070 (16334), 3076 (16394), 3209 (17293), 3538 (19116), 3548 (19116) |
| INVALID: tests still fail with the fix, three later follow-ups tried | 2627 (13282), 2828 (14686), 3677 (19578) |
| INVALID: changed test does not run before the fix (config fallbacks tried) | 3553 (19162, 19165) |
| Apps tree newer than the kernel: the pre-fix build breaks outside the test | 2704 (14152), 2794 (13159), 3342 (17848), 3688 (19594), 3735 (19900) |
| Build fails with the fix | 3174 (16990); 3382 (17569, three follow-ups tried) |
| Needs a chain of later fixes | 3248 (17517): #17784's sched/Makefile line makes it build, then the hrtimer test hangs; the rest of #17784 needs #17642 |
| Left by decision | 3297 (17642): before the fix the hrtimer module is not built at all, so a wiring line alone would "fix" it |
| Other | 2137 (10934): run_tests times out (triage omitted also_needs #10970); 2628 (13737): CONFIG_SMP_CALL dropped after the fix; 3265 (17644): generation failed |

The 22 ARM64-blocked pairs are in the viability table above. Of the 189 apps
PRs, 124 are rejected by triage (3280 on its re-triage: test_only). Three have no
verdict after a re-triage with a 45-minute agent limit (job 477961): 3347 and 3728
reach the cap again, and 646 (kernel #3272, an x86-era pair) twice gives
also_needs entries without evidence, which the verdict check rejects.

## Promoted instances (2026-10-18 evening)

30 pairs are in `apptainer/instances/nuttx/`, ids `nuttx__nuttx-<kernel PR>-a<apps PR>`
(one kernel fix can pair with several apps tests: #3299 with 649 and 651,
#14152 with 2704 and 2725). Each was validated again with that id in the
final runs `nuttx-final-arm64` (job 478151), `nuttx-final-x86` (478153) and
`nuttx-final-3759` (478177), and promoted from those runs' recipes. The 7
older Docker instances in `docker/instances/` are kept as they are.

| Host | Pairs (apps PR) |
|---|---|
| x86_64 only (`host_arch`) | 293, 646, 649, 651, 652, 912, 955, 1665, 1682, 2704, 2725, 3067 |
| aarch64 only (`host_arch`) | 2822 (fails on x86), 2137 (QEMU, validated on ARM64 only) |
| both (x86 cross-check `nuttx-crosscheck-x86`, job 478075) | 1857, 2327, 2329, 2353, 2458, 3174, 3177, 3380, 3382, 3443, 3688, 3701, 3714, 3733, 3759, 3790 |

New since the morning table, each from a general worker retry: 3688 (apps
tree of the kernel base's date), 3174 (prerequisite +#16920 adding
`nxevent_clear`), 3382 (the ostest Kconfig defining a symbol the test reads
is in the test patch), 2137 (follow-ups ranked earliest first: +#10970),
646 and 2704 (x86). 3759 carries the validator's fallback config in its
recipe. 3380 validated without its later follow-up in the final run.

Dropped: 3559 (VALID in 1 of 3 runs), 2628 (hangs at boot on ARM64; passes
before the fix on x86), 1358 and 1526 (hang with the fix and three later
follow-ups), 1962, 2627, 2828, 3677, 3248, 3342, 3347, 2794. Deferred: 3735
(apps PR merged before the kernel base, so the moved test patch is empty),
3728 (triage budget exhausted twice).

The 50 PRs whose only pairs are low confidence were sent to validation
(`nuttx-low-arm64`, `nuttx-low-x86`, `nuttx.validate_confidence` including
low). None could be generated: a low pair has no generator fields, and its
evidence records a kernel PR the triage ruled out (a rename or CI dependency,
or a change the changed tests cannot observe). They are not candidates.
