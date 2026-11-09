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
under `triage/ga/individuals/<sha>/`. The winner, `68660ee811` (13/13 on the
gold in two of three repeats), is the prompt the 189-PR run used and the one
in `docs/nuttx_apps_triage.md`. The cluster pipeline can also run a frozen
snapshot of an individual through `nuttx.individual` in its config.

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

The hand-reviewed recipes in `triage/nuttx_reviewed/`, one pair per file,
generated the first Docker instances (retired on 2026-11-08: their images
held both repositories' full history, and each pair is an Apptainer instance
in `apptainer/instances/nuttx/` now):

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
`compile_test_source` and the literal `missing_api` (the validator attests a
build failure from the fix itself; `missing_api` only names the expected
error). A recipe without a `case` field also needs `--apps-pr` and
`--kernel-pr`.

## What the agent gets

The agent sees the kernel at `/testbed` and the apps at `/testbed/apps`, each
a depth-1 repository holding only its base commit (plus the recipe's own
commits: ARM64 compatibility backports, `base_prs` prerequisites), the apps
test change applied, and the problem statement. Nothing else:

- no history, remote, tag, other ref, reflog or `FETCH_HEAD` in either
  repository (`instance.def` fetches each base by SHA at depth 1);
- no network: every container command -- agent, validation, grading -- runs
  with `apptainer exec --net --network none` (loopback only);
- no oracle hints: the task names neither the failing tests nor the files the
  fix changes;
- a problem statement written or rechecked by the served model
  (`scripts/nuttx_statement.py`) that names nothing of the fix the tests do
  not use.

`validate_instance.py` checks the image before any test
(`scripts/nuttx_leakcheck.py`: history, refs, fix commits and fixed blobs,
fix lines anywhere in the image, other repositories, the statement, the
network) and records the result in `validation.json` (`leak_check`). It also
builds the before state with `make -k` and attests a build failure from the
fix alone: at least one error in a changed test source must name something
the fix adds (a symbol absent from the starting tree, or a header the fix
creates); the proof stays in `validation.json`. `--determinism 5` judges the
before state five times, each in a fresh overlay. `promote_instance.py`
refuses a NuttX instance without a clean leak record, a 5-run determinism
record, and a statement that passed the current check.

## Running on Scholar

Triage and validation at scale run as the cluster pipeline with
`project: nuttx` in `cluster/config.yaml`:
[running_on_scholar.md](running_on_scholar.md) covers the one-time setup
(sections 3.1 and 3.2), preparing, submitting and reading a run.
`cluster/prepare.py --all-ostest` takes every ostest apps PR, and
`--verdicts-from <run>` re-validates an earlier run's verdicts without
re-triaging them.

Each phase is its own job, chained with `--after`, running a frozen copy of
the code with a clean environment. ARM64 work runs on the GPU partition
(`slurm.partition`, which takes only jobs that request a GPU), x86-only
instances on `slurm.build_partition`:

```bash
# apps PRs: triage, then validate their pairs
python cluster/prepare.py --name R --pr 2794 3342 --verdicts-from nuttx-apps-all --redo
python cluster/submit.py triage   --run R
python cluster/submit.py validate --run R --arch aarch64 --after <job>

# existing instances: rendered again with the current templates, statements
# rechecked, revalidated with the leak check and 5 before runs, evaluated
python cluster/prepare.py --name R --instances apptainer/instances/nuttx
python cluster/submit.py statement --run R
python cluster/submit.py validate --run R --arch aarch64 --after <statement job>
python cluster/submit.py validate --run R --arch x86_64  --after <statement job>
python cluster/submit.py evaluate --run R --arch aarch64 --model qwen38-nvfp4 --after <job>
python cluster/submit.py evaluate --run R --arch x86_64  --model qwen38-nvfp4 --after <job>
python scripts/promote_instance.py --run-dir <runs>/R nuttx__nuttx-<k>-a<a> ...
```

`--model` is any profile of `triage/models.json` (or a hosted model
`harness/paths.py` knows); every model gets `evaluate.step_limit` and
`evaluate.time_limit`, recorded with each result. A GPU node serves the model
itself to its own agents; an x86 evaluate job submits a model-server job of
as many GPU nodes as it has nodes (one server each, so every server has as
many agents as on a GPU node: `--max-num-seqs 4`, two agents), each x86
node reaches its own over SSH, and the evaluate job cancels the server job
when its workers are done (the account
runs two jobs at once, so the ARM64 evaluate job queues meanwhile). Each evaluation is labeled by
model and limits (`qwen38-nvfp4-50steps-80min`): its status is
`evaluate:<label>` and its trajectories, patches and grades are under
`<run>/outputs/<label>/`, so models and limits are compared on the same
instances without overwriting each other. The report counts agent outcomes
per label as pass, fail, timeout (the time limit, not the patch) and steps
(the step limit). Size the time limit from the measured time per step: with
qwen38-nvfp4 on a Spark node, two agents per node, a step took 80-100 s, so
50 steps need about 80 minutes.

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
