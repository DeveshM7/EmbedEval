# NuttX Phase 1 tuning status

Updated: 2026-10-16, after round 1.

- Bar: 13/13 on the normal gold on 3 consecutive runs of one unchanged prompt.
  Blind runs (9 cases with a must_find) are tracked only.
- Round: 1 complete; next is round 2
- Prompt: `docs/nuttx_apps_triage.md` at 1c874a2 (SHA-256 `794d31c743d40b89`)
- Consecutive perfect runs: 0/3
- Scholar jobs: 476994 cancelled; server 477004 running

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

Next: revise the prompt with general rules to search later same-code kernel
changes for required follow-ups and to recheck effective config settings;
then run round 2 normal gold and blind.
