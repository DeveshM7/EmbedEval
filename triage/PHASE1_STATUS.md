# NuttX Phase 1 tuning status

Updated: 2026-10-16, after queuing rounds 3–5.

- Bar: 13/13 on the normal gold on 3 consecutive runs of one unchanged prompt.
  Blind runs (9 cases with a must_find) are tracked only.
- Round: 1 complete; round 2 running; rounds 3–5 queued
- Queued prompt: `docs/nuttx_apps_triage.md` (SHA-256
  `fab30a5f316cc1c9b69b4d30ca260e8fd871b45e3c797e03e8fa5f6acd6f5dbc`)
- Consecutive perfect runs: 0/3
- Scholar jobs: 476994 cancelled; round 2 server 477004 running;
  round 3 **477011**, round 4 **477012**, round 5 **477013** queued

The queued code is frozen at
`/scratch/scholar/zhan5136/phase1-results/code-fab30a5f316cc1c9b69b4d30ca260e8fd871b45e3c797e03e8fa5f6acd6f5dbc/`.
It includes the candidates/nuttx records and symlinks to the scratch bare
clones. Code commit `a5c0f64` enforces a successful, dated kernel `git_log`
search on each high/medium pair's fix path before finalization; all 66 local
unit tests passed. Script commit `7ad1d24` runs both gold modes and evaluation
from the snapshot on a Spark GPU, then saves the runs and a summary.

The result root is `/scratch/scholar/zhan5136/phase1-results/`:

- `round2/{normal,blind}/` receives job 477004's outputs and scorecards when
  round 3 begins, plus `round-r2-*.log` and `summary.md`.
- `round3/`, `round4/`, and `round5/` each receive `normal/` and `blind/`
  run directories with `manifest.json`, `scorecard.json`, and `scorecard.md`,
  plus `summary.md`, client/eval logs, server log, and start/finish timestamps.
- `slurm-<job id>.out` holds the batch output. Round 3 has
  `afterany:477004`; round 4 has `afterok:477011`; round 5 has
  `afterok:477012`. All three jobs use the same frozen prompt.

Check each `round<N>/summary.md` and the normal `scorecard.json` for 13/13;
three consecutive normal 13/13 scores on the same prompt meet the pass bar.
Inspect the scorecard rows for misses, config/also_needs problems, forbidden
proposals, and exhausted budgets. Blind scores are diagnostic only. Round 3
cannot start until job 477004 ends, so no first-verdict check was possible at
submission; the snapshot script passed `bash -n`, and Slurm showed all three
dependencies pending as intended. Do not treat queued jobs as passing runs.

Round 1 attempt: normal and blind manifests were written at 17:36:48 as
`2026-10-16_173648_qwen38-nvfp4_apps{,_blind}`. Job 476994 was cancelled at
17:41:57 while both client steps were running. Neither run produced a case
verdict or scorecard, so there is no round score or streak credit. Both used
the prompt SHA above. Scholar's existing config-check cache contains completed
effective-config probes, including `CONFIG_SMP_CALL=y` kept on
`qemu-armv8a:nsh_smp` with `smp_call.c` built and called; the live
`check_config` tool and evaluator were then verified on job 477004: apps #3790
with `CONFIG_SCHED_WORKQUEUE=y` built and called `wqueue_test` on fixed and old
kernels; the evaluator scored apps #2137 1/1 with `CONFIG_SMP_CALL=y` and 0/1
without it (`CONFIG_SMP_CALL=n`, `smp_call.c` not built). An attempted negative
probe with `CONFIG_SCHED_WORKQUEUE=n` did not make that setting effectively off:
`make olddefconfig` changed it back to `y`, as the effective-config scorer
correctly observed.

Round 1 (job 477004, same prompt SHA on both runs):

- Normal `2026-10-16_174848_qwen38-nvfp4_apps`: **12/13**, must-find 9/9,
  forbidden proposals 0, budget exhausted 0. Apps #2137 proposed kernel
  #10934 but omitted required `also_needs` #10970. Its trace did not search
  later kernel commits touching the same production file and treated
  `check_config`'s build/call result as runtime evidence.
- Blind `2026-10-16_181912_qwen38-nvfp4_apps_blind`: **7/9**, must-find 9/9,
  forbidden proposals 0, budget exhausted 0. Apps #2137 again omitted
  `also_needs` #10970; apps #1665 left effective `CONFIG_SCHED_LPWORK=n`
  instead of the required `y`.
- Both clients exited 0. Neither run earns streak credit because the normal
  score was below 13/13.

Smoke run before tuning (5 cases, old prompt, no config scoring): 5/5 normal,
blind 1/2 (2327 missed: `-S` search blind to the off-by-one fix; kernel read
at the apps PR's merge date, after the fix).

Round 2 uses the revised prompt SHA above, but its normal and blind scores are
pending. Its detached client runs inside job 477004 and was left running.
