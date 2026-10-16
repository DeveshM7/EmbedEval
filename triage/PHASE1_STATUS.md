# NuttX Phase 1 tuning status

Updated: 2026-10-16, after interrupted round 1 attempt.

- Bar: 13/13 on the normal gold on 3 consecutive runs of one unchanged prompt.
  Blind runs (9 cases with a must_find) are tracked only.
- Round: 1 retry pending
- Prompt: `docs/nuttx_apps_triage.md` at 1c874a2 (SHA-256 `794d31c743d40b89`)
- Consecutive perfect runs: 0/3
- Scholar jobs: 476994 cancelled; replacement server 477004 submitted

Round 1 attempt: normal and blind manifests were written at 17:36:48 as
`2026-10-16_173648_qwen38-nvfp4_apps{,_blind}`. Job 476994 was cancelled at
17:41:57 while both client steps were running. Neither run produced a case
verdict or scorecard, so there is no round score or streak credit. Both used
the prompt SHA above. Scholar's existing config-check cache contains completed
effective-config probes, including `CONFIG_SMP_CALL=y` kept on
`qemu-armv8a:nsh_smp` with `smp_call.c` built and called; the live
`check_config` tool and evaluator still need verification on the new job.

Smoke run before tuning (5 cases, old prompt, no config scoring): 5/5 normal,
blind 1/2 (2327 missed: `-S` search blind to the off-by-one fix; kernel read
at the apps PR's merge date, after the fix).

Next: verify `check_config` and config scoring on the Spark node, then retry
round 1 (normal gold and blind) with this prompt.
