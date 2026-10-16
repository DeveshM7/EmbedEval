# NuttX PR candidates and instance generation

NuttX benchmark candidates involve two repositories. A kernel PR in
`apache/nuttx` supplies the agent's production fix. A merged PR in
`apache/nuttx-apps` may supply the executable test. A scoped link between
their PRs is a lead, not a confirmed pair.

## Current flow: script pairs, AI triages

Run `nuttx_candidates.py pairs --since ... --until ...` to collect merged apps
PRs and search merged kernel PRs that mention `nuttx-apps` in the same date
window. Starting from either repository, it follows explicit PR links and
reads cross-reference timeline events for kernel PRs it examines. It fetches
linked partners even if they merged outside the window. It keeps pairs whose
apps PR changes a `testing/ostest/` file, verifies both PRs were merged,
deduplicates the two search directions, and writes leads to
`candidates/nuttx/pairs.jsonl`. The link and ostest path are cheap clues; the
script does not decide whether the test detects the fix.

Give each pair to `triage_nuttx_prs.py` for the deeper AI judgment. The
validator then checks old-kernel/new-test failure and fixed-kernel/same-test
success for accepted, reviewed recipes. This narrow first pool can miss
unlinked pairs or tests outside ostest. Measure that limit before claiming
discovery recall.

## Earlier broader AI discovery proposal

The following was a broader AI-owned discovery proposal. It is not the
current script-first candidate workflow. It may be revisited if link-based
ostest pairing loses too many useful PRs.

1. Enumerate merged PRs from `apache/nuttx` and `apache/nuttx-apps` in bounded
   merge-date windows. Split a window when GitHub's
   [1,000-result search cap](https://docs.github.com/en/rest/search/search)
   would hide PRs, retry incomplete searches, and record every returned PR
   ID. Give the AI small batches containing each PR's title, description,
   labels, and changed paths. Fetch file paths separately; GitHub caps a PR's
   [file-list response at 3,000 paths](https://docs.github.com/en/rest/pulls/pulls),
   so flag a truncated list for review. Save each query, GitHub's reported
   count, the unique IDs received, and its incomplete-results flag. Do not
   drop PRs solely because they lack a `testing/` path or use an unfamiliar
   test framework.
2. Have the AI identify possible kernel fixes and changed executable tests in
   either repository. For each lead, inspect the relevant diff: look for a
   new or strengthened assertion, test registration, target configuration,
   and production-code change. Keep an uncertain lead for deeper inspection
   instead of silently rejecting it from a title or path list.
3. Pair leads with targeted searches: explicit cross-repository links,
   referenced issues, symbols and behavior named in the test and fix, and
   nearby merged PRs. Search from either repository. Do not compare every
   apps PR with every kernel PR. Record multiple possible partners rather
   than guessing one. A kernel PR with a changed executable test inside
   `apache/nuttx` can be a single-repository lead if its test and fix hunks
   can be separated.
4. Record each lead in a candidate ledger with source links and a status.
   Promote it to `ready_to_build` only when the AI identifies the proposed fix
   and test paths, old and fixed commits for each involved repository, a
   runnable target (`sim:nsh` or a specific QEMU target), and what failure
   the changed test should expose. Use `needs_pair`, `needs_test`, or
   `needs_fixture` when evidence is missing. The AI may propose an instance
   recipe, but a PR body saying “tests pass” is not before/after evidence.
5. Generate an instance from a reviewed recipe. Only
   `scripts/validate_instance.py` can establish the mechanical result: old
   kernel plus new test fails for the named reason; fixed kernel plus the
   same test passes. A timeout, unrelated error, or skipped test is
   inconclusive. Dataset issue and feature policies remain separate decisions.

The six validated pairs are discovery-recall checks. The six documented
rejections are provisional triage challenges, not validator-proven negatives.
Complete PR coverage does not imply perfect AI recall: audit a sample of
deferred and rejected PRs, and review misses before expanding from a bounded
date window to the full history.

The current `triage_nuttx_prs.py` classifies a supplied apps/kernel pair or a
cached gold record; it does not perform the AI-owned search above.

For a discovery pass, give the AI an unfiltered batch from both repositories.
For **each** PR, it records `fix_lead`, `test_lead`, `both`, or `no_lead`, with
one reason and the evidence it actually saw. A lead triggers a targeted search
across repository and batch boundaries. Before proposing a pair, the AI opens
both diffs and records the changed test assertion and registration, production
fix hunk, relevant issue, and missing evidence. It may leave a lead as
`needs_pair` or `needs_test`. This is a discovery instruction, not an operation
supported by the current model client: that client receives already enriched
records and has no GitHub search tool. Hide the gold labels when measuring
discovery recall.

## Two-way discovery pilot (October 8, 2026)

An AI first pass read the title and description preview of every merged PR
from **September 1–14, 2026**: 13 in `apache/nuttx-apps` and 104 in
`apache/nuttx`. GitHub returned all 117 unique PR IDs with
`incomplete_results=false`. The collection queries used only repository,
merged state, and date; no path, keyword, or link rule selected the leads.
They were `repo:apache/nuttx-apps is:pr is:merged
merged:2026-09-01..2026-09-14` and the same query with
`repo:apache/nuttx`.
Changed-file diffs were opened for the leads below. This was a metadata pilot,
not a full-diff review of all 117 PRs or a model-accuracy measurement.

| Starting lead | Follow-up | Triage finding |
|---|---|---|
| [apps #3771](https://github.com/apache/nuttx-apps/pull/3771): new `test_others_chksum()` | [kernel #20058](https://github.com/apache/nuttx/pull/20058), linked in its discussion and sharing [issue #20010](https://github.com/apache/nuttx/issues/20010) | Promising test/fix pair. The apps diff adds and registers a CMocka test comparing an IOB chain containing an empty fragment with contiguous data. The kernel diff adds an early return for `len == 0` in `net/utils/net_chksum.c`. PR descriptions claim fail-before/pass-after; we have not reproduced either result. The current generator only runs `ostest`, so this `nettest` case needs a runner/template before instance validation. |
| [kernel #20112](https://github.com/apache/nuttx/pull/20112): restore `-ENOENT` from `work_cancel()` | A targeted apps search for `work_cancel` found [apps #3790](https://github.com/apache/nuttx-apps/pull/3790), merged **after** the review window | Promising test/fix pair. The apps diff strengthens the idle-work assertions in `testing/ostest/wqueue.c` to require `-ENOENT`; it also broadens separate race-sensitive assertions to allow `OK` or `-ENOENT`. The kernel diff changes the return value in `sched/wqueue/kwork_cancel.c`. The `wqueue_test()` call is present in `ostest_main.c`; target configuration and old-fail/fixed-pass behavior still need checking. |
| [kernel #20129](https://github.com/apache/nuttx/pull/20129): change `atexit()` exit-handler processing | Search apps for `atexit` | Fix lead only. Its PR body shows a scratch test in `apps/examples/hello`, but the merged kernel diff changes only `libs/libc/stdlib/lib_atexit.c`. The apps search did not identify a matching merged regression-test PR. Keep as `needs_test`, not a validated negative. |

The first pair demonstrates apps-to-kernel discovery. The second demonstrates
kernel-to-apps discovery across the batch boundary. A shared symbol or PR link
started each investigation; changed assertions and production diffs supplied
the stronger pairing evidence. Neither pair has been independently validated
by this pilot, and neither has been added to `triage/nuttx_gold.json`.

## Build the linked ostest pair pool

```sh
python scripts/nuttx_candidates.py pairs \
  --since 2024-03-01 --until 2024-03-31

# Start from one known kernel PR or apps PR instead:
python scripts/nuttx_candidates.py pairs --kernel-pr 8913
python scripts/nuttx_candidates.py pairs --pr 1682
```

The date-window command fetches apps PRs and their changed-file lists itself.
The kernel-first command fetches the linked apps PRs; it needs no preloaded
apps cache. It reads NuttX PR comments and timeline cross-references, recording
`kernel_timeline` when a `nuttx-apps` PR generated a mention. A bare `pairs`
command still reads the cumulative apps cache for older workflows. All modes
record where each exact link appeared. Search
windows above GitHub's 1,000-result limit or marked incomplete stop instead of
silently returning a partial pool. The date-window command reads timelines
only for kernel PRs returned by its `nuttx-apps` search; a kernel PR with only
an unindexed timeline link can still be missed. Unlinked pairs and tests
outside ostest can also be missed. Apps #2327 can
link more than one kernel PR; AI triage selects the actual production fix. The
older `filter`/`enrich` commands remain available for broader `testing/` leads
and full PR records.

Six older pairs pass the discovery self-check. This establishes that the
filter and link extraction retain those examples; it does not establish that
every older instance is valid. In particular, an originating GitHub issue is
useful evidence but cannot be a NuttX hard filter: the live-valid #11898 pair
has no closing issue reported for its kernel PR. Record that provenance gap
for review before treating a mechanically valid pair as dataset eligible.
Kernel #8885 does cite [issue #8869](https://github.com/apache/nuttx/issues/8869)
in plain `issue #N` form; enrichment resolves that shorthand within the kernel
repository.

## Review and generate

Read `candidates/nuttx/enriched/<apps-pr>.json` and decide which kernel PR,
test paths, and fix paths actually form a separable task.

The pair-triage prompt is in `docs/nuttx_pr_triage.md`. Run and score
the 14-case gold set with the same served model profiles used by Zephyr. First
prepare the thirteen apps records in the checkout where the model client runs;
the generated `candidates/` cache is ignored by Git:

```sh
python scripts/filter_candidates.py enrich --repo nuttx --pr \
  1665 1682 1792 1857 2137 2327 2329 2458 2725 3401 3676 3767 3785
```

For one already identified pair, supply both PR numbers directly. This fetches
only those PRs if they are not cached and writes a separate pair record; it
does not search for a partner. A pair-only run does not produce a gold
scorecard:

```sh
.venv/bin/python scripts/triage_nuttx_prs.py run \
  --profile qwen38-nvfp4 --apps-pr 2327 --kernel-pr 11889
```

To triage every distinct pair in a discovered pool, supply its JSONL file:

```sh
.venv/bin/python scripts/triage_nuttx_prs.py run \
  --profile qwen38-nvfp4 --pairs-file candidates/nuttx/pairs.jsonl
```

This run records the pair cases in its manifest. Pair-pool verdicts are not
gold labels; use the separate `--gold` run to measure classification accuracy.

Then start the server and client on the same Scholar compute node:

```sh
sbatch scripts/serve_triage_model.sh qwen38-nvfp4
srun --jobid=<id> --overlap -c 4 .venv/bin/python \
  scripts/triage_nuttx_prs.py run --profile qwen38-nvfp4 --gold
.venv/bin/python scripts/triage_nuttx_prs.py eval triage/nuttx_runs/<run>
```

The run stores a prompt snapshot, manifest, and one verdict and full trace per
case in `triage/nuttx_runs/`. A run with `--gold` also writes a scorecard. The
gold file is `triage/nuttx_gold.json`: seven pairs with native validation traces and
seven rejections, six based on PR evidence and one (apps #2137) on a validator
run whose fixed kernel does not link on `sim:nsh`. Only the accept/reject choice is
scored. The selected kernel PR is reported as a secondary check. The AI
verdict includes a detecting test, expected failure, and `readiness` status.
`ready_to_generate` means each changed test is called from `ostest_main.c`
and its required config is listed, so the verdict can feed the generator
directly. `needs_reviewed_recipe` still needs a reviewer to confirm the
changed test runs on `sim:nsh` before generation;
`needs_fixture` needs runner or target work. None is validation.
The kernel-only rejection #20091 is fetched directly at run time because it
does not enter the apps-first discovery cache.

The generator-ready verdict used by the current `sim:nsh` generator has this
shape (abbreviated):

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

For compile failures, set `fail_type` to `compile_error` and add
`compile_test_source` plus the literal `missing_api`. For example, #12802
needs `CONFIG_SCHED_EVENTS=y` in `config_overrides`. Inspect the actual apps
test hunks: selecting test filenames alone does not prove every hunk is
test-only.

```sh
python scripts/generate_nuttx_instance.py \
  --triage triage/nuttx_runs/<run>/apps-2329-kernel-11898.json

python scripts/build_bases.py --repo nuttx
EMBEDEVAL_INSTANCES_DIR=generated python scripts/build_instances.py \
  --instance nuttx__nuttx-11898
EMBEDEVAL_INSTANCES_DIR=generated python scripts/validate_instance.py \
  nuttx__nuttx-11898 --verbose
```

Supply a generator-ready triage JSON, or a reviewed recipe with the fields
shown above. For older reviewed recipes without a `case` field, also pass
`--apps-pr` and `--kernel-pr`. Its `accept` value authorizes generation for the
mechanical comparison; it does not settle the
benchmark's issue-provenance or bug-fix eligibility policy. Re-enrich the pair
before regenerating if upstream PR metadata or the local cache changes.

Generation diffs the apps PR branch's base-to-tip commits only for the
reviewed test paths, checks that the patch applies at the apps base commit,
and records the kernel PR branch's base-to-tip range for the validator's fix
patch. Merge commits are not used as diff endpoints because they can include
unrelated changes. By default the instance goes to `generated/` with a
`-gen` image tag, apart from the checked-in instances. A validated instance
joins the benchmark with `--out docker/instances --tag-suffix=` (the standard
`embedeval:nuttx-<PR>` tag); the reviewed inputs for the checked-in NuttX
instances are in `triage/nuttx_reviewed/`.

Validation must show old kernel plus the new apps test fails for the intended
reason and fixed kernel plus the *same* apps test passes. A timeout, unrelated
compile error, missing test output, or runner crash is inconclusive. The
current Docker template runs native `sim:nsh` and the whole `ostest` suite to
completion, then reads its log once: pass needs `user_main: Exiting`,
`ostest_main: Exiting with status 0`, no `ERROR` line, and the
`user_main: ... test` banner of each changed test (derived by the generator
from the patched `ostest_main.c`); anything else that completes fails, and a
timeout returns 2. An unrelated `ostest` failure at the base commit therefore
fails the fixed run too, which validation reports. Triage accepts only
pairs that run on `sim:nsh`. A pair whose test can only run on a QEMU board is
rejected with a `qemu_lead` (board, confidence, why);
`triage_nuttx_prs.py leads` collects them from all runs into
`triage/nuttx_qemu_leads.jsonl`, to revisit once a QEMU template and runner
exist.

## Running NuttX triage on Scholar

Triage runs on Scholar's Spark nodes against a vLLM model server; validation
does not (there is no Docker there; NuttX instances are validated locally).
The one-time setup -- `gh`, the Python venv, the vLLM image and the model
weights -- is the same as for the Zephyr pipeline:
[running_on_scholar.md](running_on_scholar.md), sections 3.1 and 3.2. That
guide clones into `~/code/EmbedEval`; `scripts/scholar_triage_remote.sh`
expects `~/EmbedEval` unless `SCHOLAR_REPO` says otherwise.

`scripts/scholar_triage_remote.sh` drives the NuttX triage from your own
machine over SSH (`serve`, `status`, `sync`, `run <job>`, `wait`, `pull`).

1. **Key-based SSH, once.** From your laptop's own terminal (it prompts for
   BoilerKey and Duo, so not from an agent's shell):
   `ssh-copy-id -i ~/.ssh/id_ed25519.pub scholar.rcac.purdue.edu`. After that
   `ssh -o BatchMode=yes scholar.rcac.purdue.edu hostname` works without a
   prompt; the key also replaces BoilerKey for later logins.
2. **Run the one-time setup on a Spark node**, not the x86 login node:
   `srun -A scholar -p spark-interactive …` or `srun --jobid=<job> --overlap`
   into a job you already have. `gh auth login` still needs you once (it shows
   a device code; approve it in any browser).
3. **The two repositories the NuttX triage tools read** are full bare clones,
   linked where the script looks for them:
   ```bash
   git clone --bare https://github.com/apache/nuttx.git      $SCRATCH/repos/nuttx.git
   git clone --bare https://github.com/apache/nuttx-apps.git $SCRATCH/repos/nuttx-apps.git
   ln -sfn $SCRATCH/repos/nuttx.git      ~/EmbedEval/candidates/nuttx/kernel.git
   ln -sfn $SCRATCH/repos/nuttx-apps.git ~/EmbedEval/candidates/nuttx/apps.git
   ```
   Copy `candidates/nuttx/enriched/` up from the machine that enriched it
   (it is gitignored) so triage and the generator read the same records.
4. **Then:** `serve` (one or two server jobs; two use both job slots), `status`
   until each says ready, `sync`, `run <job>` for each server, `wait`, `pull`.

What bit us, so the script handles it:

| Symptom | Cause |
|---|---|
| `Exec format error` in a job or `srun` step | `sbatch`/`srun` copy the login node's environment; an x86 `conda init` in `~/.bashrc` put `/apps/cent7/anaconda` first on `PATH`. The script drops those entries; guarding the `conda init` block with `if [ "$(uname -m)" = x86_64 ]` fixes interactive shells. |
| `srun` runs the command 10 times | A job with 10 tasks; pass `-n 1` to `srun --overlap`. |
| `ssh` does not return until the run ends | The `srun` step held the session; start it as `nohup setsid bash -c '…' & disown`. |
| A "wait" loop never ends | `pgrep -f pattern` through `ssh` matches the ssh command itself; write the pattern as `[t]riage…`. |
| A third job waits with `AssocMaxJobsLimit` | The account runs 2 jobs at once (`sacctmgr show assoc user=$USER`): MaxJobs 2, MaxSubmit 50, MaxWall 4 h. |

## Workflow coverage

| Stage | NuttX status |
|---|---|
| PR fetch and linked ostest pairing | Scripted pool from ostest diffs and links in either direction; recall beyond linked ostest pairs is not measured |
| AI triage instructions and model scoring | Pair classifier and 14-case scoring implemented; live model accuracy is not yet measured |
| Reviewed verdict to instance files | Implemented for `sim:nsh`/`ostest`, with explicit test/fix paths and branch commits |
| Native Docker image and whole-suite runner | Implemented; optional config settings and clean rebuild are recorded in metadata, changed-test banners are derived |
| Old-fail/fixed-pass validation | Implemented through the shared validator; each new generated instance still needs a recorded local run |
| QEMU-specific image and runner | Awaiting a concrete candidate that needs QEMU; discovery does not exclude it |
| Unlinked-pair discovery (merge-window + symbol overlap) | Not implemented; a possible future option if linked-pool recall proves insufficient, see above |

The next small milestones are: check that the scripted pair pool rediscovers
the six validated pairs; score the AI triage on that pool and the provisional
rejects; then inspect fresh candidates and run instance validation. Measure
the linked-ostest pool's misses before broadening it. Agree on issue
provenance and whether feature additions qualify before final dataset
admission. Add a QEMU image
and runner when a specific retained candidate needs that target, followed by
the same named fail-before/pass-after validation.

The current pair workflow requires an apps PR with an ostest change, even when
discovery starts from a kernel PR. A kernel PR with an executable regression
test only inside `apache/nuttx`, and no apps PR, needs a separate single-repo
pass. This has not been implemented or measured against known examples; it
would scan kernel PR file lists directly, not compare every kernel PR with
every apps PR.

## Possible future option: unlinked pairs (not implemented)

Every pairing method above requires some trace — a body/comment reference,
timeline cross-reference, or a shared issue. A kernel fix and an apps test merged
independently, with neither ever mentioning the other, would not surface in
either direction. Not a current priority; recorded here in case the linked
pool stops producing new pairs.

The shape such a pass would take: restrict to apps PRs that already cleared
`classify_apps` but found no link, search `apache/nuttx` merges in a date
window around each one's `merged_at` (`search_merged`, already used
elsewhere), then rank candidates by identifier overlap between lines **added**
in the apps test diff and lines **added** in the kernel diff — `full_pr`
already stores each file's `patch` text, so no new fetching is needed. Raw
overlap is close to useless alone: common tokens (`ret`, `errno`, `len`) match
constantly across unrelated PRs. It would need a rarity weighting step first —
an identifier frequency table built from several hundred merged kernel PRs,
counting a shared token as evidence only when it is rare in that corpus (e.g.
`SIGSET_FMT`, not `status`).

Any pairs this produced would need their own ledger, separate from
`pairs.jsonl` — e.g. `weak_pairs.jsonl` with `link_sources: []` and a
`confidence: "symbol_match"` field plus the matched symbols, so a reviewer
sees why a pair was suggested and nobody mistakes a heuristic guess for a
textual link when later computing discovery recall. It would still go through
the same `nuttx_pr_triage.md` rubric and `validate_instance.py` fail/pass
check as any other lead — this only widens discovery, it does not change what
counts as an accepted instance.
