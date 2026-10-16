# NuttX Phase 1 tuning status

Updated: 2026-10-16, before round 1.

- Bar: 13/13 on the normal gold on 3 consecutive runs of one unchanged prompt.
  Blind runs (9 cases with a must_find) are tracked only.
- Round: 1 (not started; waiting for a Slurm slot)
- Prompt: `docs/nuttx_apps_triage.md` at ed2638f
- Consecutive perfect runs: 0/3
- Scholar jobs: 476994 (phase1-server, pending, AssocMaxJobsLimit)

Smoke run before tuning (5 cases, old prompt, no config scoring): 5/5 normal,
blind 1/2 (2327 missed: `-S` search blind to the off-by-one fix; kernel read
at the apps PR's merge date, after the fix).

Next: verify `check_config` and config scoring on the Spark node, then run
round 1 with the prompt that judges "before" at the apps PR's base, searches
paths and diffs, checks config and follow-up PRs.
