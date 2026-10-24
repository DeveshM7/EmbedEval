# NuttX PR triage and instance generation

A NuttX instance involves two repositories. A merged kernel PR in
`apache/nuttx` supplies the production fix the agent must write. A merged PR
in `apache/nuttx-apps` supplies the executable test, a change under
`testing/ostest/`. The unit of work is the apps PR: triage finds the kernel PRs
its test checks, and validation proves the pair.

```sh
# apps-only records (PR, commits, comments, timeline, test sources)
python scripts/nuttx_candidates.py enrich-apps --ostest-scan
# triage: --apps-pr N..., --gold, or --all-ostest; --blind hides links
python scripts/triage_nuttx_prs.py apps --profile qwen38-nvfp4 --gold
python scripts/triage_nuttx_prs.py eval triage/nuttx_runs/<run>
# build a proposed high or medium pair, then validate it
python scripts/generate_nuttx_instance.py \
  --triage triage/nuttx_runs/<run>/apps-3790.json --kernel-pr 20112
# after validation: valid pairs join must_find, failures go to needs_review
python scripts/triage_nuttx_prs.py gold-promote generated/nuttx__nuttx-20112
```

At scale, the same steps run as the cluster pipeline on Scholar (below).

## Triage

The model gets one merged apps PR that changes `testing/ostest/` (189 of
them, from `candidates/nuttx/apps_merged_file_scan.json`), classifies it and
finds the kernel PRs itself. The prompt is `docs/nuttx_apps_triage.md`.

Its tools read either repository at the apps PR's base or head, a kernel
PR's base or head, any commit, or the default branch as of a date;
`git_log` searches history by date, symbol (`-S`), regex (`-G`), message and
path; `open_ref` follows PR, issue and commit references (through `gh api`,
cached in `candidates/nuttx/github/`) and maps a commit to its merged PR;
`check_config` reports which `#if` branches of the changed test a config
compiles. Each apps PR has a budget, by default 40 tool calls and 30 minutes;
a case that runs out without a valid verdict is recorded as
`budget_exhausted`. `git_log` with `symbol` over the whole kernel history
takes about two minutes on Scholar, so the model is told to narrow it.

A verdict gives a `kind` (`kernel_test`, `test_only`, `apps_fix`,
`qemu_only`), `kernel_search` (`found` or `nothing_found`), what was
`searched`, and `pairs`, each with a confidence and evidence. High and
medium pairs carry the generator fields and go to generation and
validation; low ones are only recorded. A run writes `pairs.jsonl`, one line
per pair, and `qemu_leads.jsonl` for pairs that need a QEMU board.

The gold `triage/nuttx_apps_gold.json` is partial: per apps PR a kind,
`must_find` (known valid pairs, required at high or medium) and
`must_not_propose` (known wrong pairs, forbidden at high or medium). Other
proposals are unknown: not penalized, listed in the scorecard for
validation. A `--blind` run strips kernel links, kernel URLs, commit hashes
and the known partner numbers from the record and from apps-side tool
output, to measure search without links; it is scored separately.

### Prompt tuning

`scripts/nuttx_ga.py` searches prompt and sampling variants against the gold:
each individual is the base prompt (`triage/ga/base_prompt.md`) plus named
rules from `triage/ga/rules.json` and knob settings, stored with its prompt
under `triage/ga/individuals/<sha>/`. The variant the 189-PR run used is
`68660ee811` (13/13 on the gold in two of three repeats); the cluster pipeline
runs a frozen snapshot of it through `nuttx.individual` in its config.

## Generate and validate

`generate_nuttx_instance.py` takes a triage verdict and one of its pairs,
diffs the apps PR branch's base-to-tip commits for the changed test paths,
checks that the patch applies at the apps base commit, and records the kernel
PR branch's base-to-tip range for the validator's fix patch. Merge commits
are not used as diff endpoints because they can include unrelated changes.
Follow-up kernel PRs the fix also needs (`also_needs`) are applied with it in
merge order; prerequisites the test needs (`base_needs`) go into the base, so
the pre-fix run fails for the bug rather than for their missing names. By
default the instance goes to `generated/`.

Validation must show that the old kernel plus the new apps test fails for the
intended reason, and the fixed kernel plus the *same* apps test passes. The
runner runs the whole `ostest` suite to completion, then reads its log once:
a pass needs `user_main: Exiting`, `ostest_main: Exiting with status 0`, no
`ERROR` line, and the `user_main: ... test` banner of each changed test
(derived by the generator from the patched `ostest_main.c`). Anything else
that completes fails, and a timeout returns 2. A `compile_error` pair instead
fails before the fix with its `missing_api` in the build log. An unrelated
`ostest` failure at the base commit therefore fails the fixed run too, which
validation reports.

Pairs run on the native simulator `sim:nsh` or on a QEMU board;
`scripts/nuttx_platforms.py` maps each target to its image and QEMU command
line (see [nuttx_qemu.md](nuttx_qemu.md)). Kernels from before the
simulator's ARM64 host support, and bugs that exist on one host only, are
validated on x86_64 and promoted with `host_arch`. A valid instance joins
the benchmark with `scripts/promote_instance.py` (into
`apptainer/instances/nuttx/`).

### Reviewed recipes

The 7 Docker instances in `docker/instances/` were generated from the
hand-reviewed recipes in `triage/nuttx_reviewed/`, one pair per file:

```json
{
  "verdict": "accept",
  "case": "apps-2329-kernel-11898",
  "kernel_pr": 11898,
  "readiness": "ready_to_generate",
  "platform": "sim:nsh",
  "fail_type": "runtime_assert",
  "problem_statement": "Describe the missing kernel behavior",
  "test_paths": ["testing/ostest/cancel.c"],
  "fix_paths": ["sched/pthread/pthread_join.c"],
  "fail_to_pass": ["cancel_test"],
  "pass_to_pass": [],
  "config_overrides": {}
}
```

For compile failures, `fail_type` is `compile_error` with
`compile_test_source` and the literal `missing_api`. Regenerate one with
`--out docker/instances --tag-suffix=` (the standard `embedeval:nuttx-<PR>`
tag); a recipe without a `case` field also needs `--apps-pr` and
`--kernel-pr`.

## Running on Scholar

Triage and validation at scale run as the cluster pipeline with
`project: nuttx` in `cluster/config.yaml`:
[running_on_scholar.md](running_on_scholar.md) covers the one-time setup
(sections 3.1 and 3.2), preparing, submitting and reading a run.
`cluster/prepare.py --all-ostest` takes every ostest apps PR, and
`--verdicts-from <run>` re-validates an earlier run's verdicts without
re-triaging them.

The triage tools read full bare clones of both repositories, linked where the
scripts look for them:

```bash
git clone --bare https://github.com/apache/nuttx.git      $SCRATCH/repos/nuttx.git
git clone --bare https://github.com/apache/nuttx-apps.git $SCRATCH/repos/nuttx-apps.git
ln -sfn $SCRATCH/repos/nuttx.git      ~/EmbedEval/candidates/nuttx/kernel.git
ln -sfn $SCRATCH/repos/nuttx-apps.git ~/EmbedEval/candidates/nuttx/apps.git
```

| Symptom | Cause |
|---|---|
| `Exec format error` in a job or `srun` step | `sbatch`/`srun` copy the login node's environment; an x86 `conda init` in `~/.bashrc` put `/apps/cent7/anaconda` first on `PATH`. Guard the `conda init` block with `if [ "$(uname -m)" = x86_64 ]`. |
| `srun` runs the command 10 times | A job with 10 tasks; pass `-n 1` to `srun --overlap`. |
| A third job waits with `AssocMaxJobsLimit` | The account runs 2 jobs at once (`sacctmgr show assoc user=$USER`): MaxJobs 2, MaxSubmit 50, MaxWall 4 h. |

## Limits

Only apps PRs that change `testing/ostest/` are searched. A kernel PR whose
regression test lives inside `apache/nuttx` with no apps PR, and tests in
other apps programs (e.g. `testing/drivers/`), need a separate pass that has
not been built.
