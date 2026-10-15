# Running the Zephyr pipeline on Scholar

This guide is for anyone who wants to turn merged Zephyr pull requests into
validated benchmark instances on Purdue Scholar's Spark nodes. It covers
what the pipeline does, how to set it up, how to start a run, what a run
produces, and how to review and promote the results.

How the parts work together, and how to change them, is in
[pipeline_internals.md](pipeline_internals.md).

Paths below use `$SCRATCH` for `/scratch/scholar/$USER`.

---

## 1. What the pipeline does

An **instance** is one benchmark task: a Zephyr pull request whose tests fail
at the commit before the fix and pass once the fix is applied, packaged as an
Apptainer image an agent can work in (see the [README](../README.md)). The
pipeline finds such PRs automatically and builds and checks each instance.

```
 merged PRs on GitHub
   │
   ▼  1. fetch          title, files, labels                           GitHub API
   ▼  2. hard filter    drops reverts, PRs touching no tests/, ...     free, instant
   ▼  3. enrich         diffs, review threads, test suites, sources,   GitHub + local clone
   │                    and the PR's era: Python, SDK, boards
   ▼  4. dry run        twister's own filters: which scenario/board    seconds per PR
   │                    pairs could run on our boards? none -> drop
   ▼  5. triage         an LLM reads the whole record and accepts or   ~10 min per PR, GPU
   │                    rejects; picks a pair, writes the problem
   │                    statement and the tests that must flip
   ▼  6. generate       the Apptainer recipe, from the verdict         seconds
   ▼  7. build          the instance image (2-5 GB)                    10-20 min
   ▼  8. validate       tests FAIL as built, PASS with the upstream    a few min
   │                    fix  ->  VALID or INVALID
   ▼  9. review         you read the report and the instances          by hand
   ▼ 10. promote        a VALID instance is copied into                by hand
                        apptainer/instances/, the committed benchmark
```

Steps 1-4 run in **prepare**, on the node you are logged in to. Steps 5-8
run in **one Slurm job** over several Spark nodes. Steps 9-10 are yours.

How much survives each step, from the runs so far (late-2025 PRs):

| Step | Kept |
|---|---|
| hard filter | about 12% of merged PRs (67 of 551) |
| dry run | about 55% of those (34 of 62 reached triage; 25 had no runnable pair, 3 errors) |
| triage | about 40% accepted (13 of 34) |
| validation | about 85% VALID (11 of 13) |

So roughly **2 in 100 merged PRs become an instance**. A month of Zephyr
(about 500 merged PRs) gives on the order of 10.

The board an instance runs on is one of: `native_sim/native/64` (and its
older names), `qemu_x86`, `qemu_x86_64`, `qemu_cortex_m3`, `mps2/an385`,
`qemu_cortex_a53`, `qemu_riscv32`, `qemu_riscv64`. These are the boards
that run on an ARM64 machine with no hardware attached.

---

## 2. Where it runs, and the limits

Everything runs on Scholar's **Spark nodes**: one NVIDIA GB10 (a Blackwell
GPU with about 120 GB of memory shared with the CPU) and 20 ARM64 cores
each. There is no Docker and no root; containers run with **Apptainer**.

```
 ┌──────────────────────────────┐   sbatch    ┌───────────────────────────────────┐
 │ your interactive Spark node  │────────────►│ spark-batch nodes (1-16 of them)  │
 │ (OnDemand "Spark" desktop)   │             │  one job, one worker per node:    │
 │                              │             │   triage: its own model server    │
 │  setup.py, prepare.py,       │             │   validation: builds, validates   │
 │  submit.py, report.py,       │             └───────────────────────────────────┘
 │  promote_instance.py         │                      │
 └──────────────────────────────┘                      │
          │                                            │
          ▼                                            ▼
   /home/$USER  (code, .venv)          /scratch/scholar/$USER  (everything else)
          └──────── network file systems, the same on every node ────────┘
```

Work from an **interactive Spark session** (an OnDemand Spark desktop),
not an `ssh` login shell: the Spark nodes are ARM64, and the `.venv`, the
images and the dry runs must match that.

| Limit | Value | What it means for you |
|---|---|---|
| Running jobs per account | **2** | Your desktop is one. So **one pipeline job at a time.** |
| Job time | **4 h** max | The config uses 3 h; a run that does not finish is resumed (section 8). |
| `spark-batch` nodes | 20 (`scholar-k060`-`k079`) | A job takes up to 16 (`nodes_max`). |
| Memory per batch job | 120.5 GB | Covers the model too: models over ~50 GB of weights need `spark-batch`, never the desktop. |
| Memory on the desktop | 60 GB | Enough for prepare; not for a model server. |
| Home | 25 GB | Code and `.venv` only. |
| Scratch | 1 TB | Images, runs, model weights. |
| GitHub API | needs a login | Unauthenticated calls are used up for Scholar's shared address; `gh auth login` once. |

Disk a run needs: the shared images (about 9 GB, once, for all runs), a few
MB per PR of records and logs, and up to 4 instance images per node at once
(2-5 GB each), each deleted as soon as it has been validated. The **disk
guard** stops new builds if a run's images ever exceed 250 GB.

Times, for planning:

| | Time |
|---|---|
| prepare, 20 PRs | about 1 min (enrich and dry run, several at once) |
| prepare, 100+ PRs | a few minutes, plus about 10 min per missing SDK image |
| model server start | about 10 min the first time (it compiles), about 5 later |
| triage, one PR | 3-27 min, typically 7-11; 4 at once per node |
| validation, one PR | 14-28 min (one build); 4 at once per node |
| the 19 gold PRs on 4 nodes | 65 min |
| 62 PRs on 16 nodes | about 1 h 30 min |

---

## 3. One-time setup

### 3.1 The tools

**GitHub CLI**, for the GitHub API:

```bash
V=$(curl -sL -o /dev/null -w '%{url_effective}' https://github.com/cli/cli/releases/latest | sed 's#.*/v##')
curl -sL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${V}/gh_${V}_linux_arm64.tar.gz"
tar xzf /tmp/gh.tgz -C /tmp && mkdir -p ~/.local/bin && cp /tmp/gh_${V}_linux_arm64/bin/gh ~/.local/bin/
gh auth login            # GitHub.com -> HTTPS -> browser; enter the code at github.com/login/device
```

Put `~/.local/bin` on your `PATH` (in `~/.bashrc`). The login is stored in
`~/.config/gh/`, once per account.

**The code and its Python environment**, from your Spark desktop:

```bash
git clone -b feature/pr-filtering https://github.com/DeveshM7/EmbedEval.git ~/code/EmbedEval
cd ~/code/EmbedEval
module load conda/2026.06
python -m venv .venv
.venv/bin/pip install -r requirements.txt huggingface_hub hf_transfer
```

(The pipeline is on the `feature/pr-filtering` branch until it is merged.)
Every command below is run from `~/code/EmbedEval` with `.venv/bin/python`
(or with the venv activated).

### 3.2 The model and its server image

Triage uses an open-weights model served by **vLLM**, run from vLLM's own
container image. Both are large and go on scratch, outside the run folders,
because you may use them for other things too.

```bash
mkdir -p $SCRATCH/{images,hf,logs}
cd $SCRATCH/images
APPTAINER_CACHEDIR=$SCRATCH/apptainer-pull APPTAINER_TMPDIR=/tmp \
    apptainer pull vllm-0.27.1.sif docker://vllm/vllm-openai:v0.27.1     # ~10 GB download, 7.8 GB file
rm -rf $SCRATCH/apptainer-pull                                           # the download cache, no longer needed

HF_HOME=$SCRATCH/hf/.cache ~/code/EmbedEval/.venv/bin/hf download \
    Inferact/Qwen3.8-27B-NVFP4 --local-dir $SCRATCH/hf/models/Qwen3.8-27B-NVFP4   # 25 GB
```

Apptainer's download cache defaults to your home, which is too small for
it; hence the cache on scratch, deleted once the image exists.

### 3.3 Your settings

```bash
cp cluster/config.example.yaml cluster/config.yaml
```

`cluster/config.yaml` is yours and is not in git. Anything you leave out
takes the value in `config.example.yaml`; for most people the copy needs no
edits, since the defaults are the paths used above. The settings you are
most likely to change:

| Setting | Default | Change it when |
|---|---|---|
| `scratch_root` | `$SCRATCH/embedeval` | you want the data somewhere else |
| `paths.models`, `paths.vllm_image` | as in 3.2 | your weights or image are elsewhere |
| `slurm.nodes_max` | 16 | you want to leave nodes for others |
| `slurm.time_limit` | 3 h | never above 4 h |
| `model.profile` | `qwen38-nvfp4` | you use another model (pipeline_internals.md, 7.3) |

All settings are explained in [pipeline_internals.md](pipeline_internals.md), section 7.

### 3.4 `setup.py`

```bash
.venv/bin/python cluster/setup.py
```

It checks your machine and creates the shared folders. The first time it
clones Zephyr (about 1 GB, a minute) and builds the three base Python images
(a few minutes). It never deletes or overwrites anything, so run it again
whenever you like. Good output looks like this:

```
EmbedEval setup -- config: /home/you/code/EmbedEval/cluster/config.yaml
  scratch root: /scratch/scholar/you/embedeval

  [  ok   ] architecture: aarch64
  [  ok   ] apptainer: apptainer version 1.4.5
  [  ok   ] gh (GitHub API for enrichment): logged in
  [  ok   ] Slurm partition spark-batch: accepts jobs
  [ found ] model weights (qwen38-nvfp4): /scratch/scholar/you/hf/models/Qwen3.8-27B-NVFP4
  [ found ] vLLM image: /scratch/scholar/you/images/vllm-0.27.1.sif
  [ note  ] vLLM compile cache: not filled yet; the first server start fills it (about 8 min slower, once)
  [ found ] shared/: /scratch/scholar/you/embedeval/shared
  [ found ] runs/: /scratch/scholar/you/embedeval/runs
  [ found ] Zephyr clone: /scratch/scholar/you/embedeval/shared/zephyr.git
  [ found ] base image Python 3.10: python-3.10-e3297b8d.sif
  [ found ] base image Python 3.9: python-3.9-e3297b8d.sif
  [ found ] base image Python 3.12: python-3.12-e3297b8d.sif

13 ok, 0 failed
```

Each `FAIL` line says what to do. The usual ones:

| FAIL | Fix |
|---|---|
| architecture | you are on an x86 login node; use a Spark desktop |
| gh | `gh auth login` (3.1) |
| Slurm partition | your account has no access to `spark-batch`; ask the cluster admins |
| model weights / vLLM image | download them (3.2), or point `paths.*` at them |
| Zephyr clone: left from an interrupted clone | delete the `.partial` folder it names, run again |

---

## 4. What a run is

A **run** is one batch of PRs taken through the pipeline: a name, and a
folder `$SCRATCH/embedeval/runs/<name>/` holding everything about it --
the plan, every record, verdict, recipe, log and the report. Runs never
share files with each other (apart from the shared images), so you can keep
as many as you like and delete one by deleting its folder.

A run goes through these steps, each a command:

```
 you, on your desktop                       Slurm, on 1-16 batch nodes
 ─────────────────────────────────────────  ───────────────────────────────────────────
 prepare.py  ──►  manifest.json
 (fetch, filter, enrich, dry run)
 submit.py   ──►  jobs/pipeline-*.sbatch ──► phase 1  triage       (each node its share)
                                             phase 2  validation   (each node its share)
                                             phase 3  report.md
 report.py   ◄──  (any time, even while the job runs)
 review, then promote_instance.py  ──►  apptainer/instances/ in the repository
```

- **prepare** works out what there is to do and records it in
  `manifest.json`: every PR, and where it stopped if it stopped early.
- **submit** starts one job for whatever is still to do. Running it again
  later resumes the run.
- **The job** splits the PRs over its nodes. In phase 1 every node starts
  its own model server and triages its share; in phase 2 every node builds
  and validates its share of the accepted PRs; phase 3 writes the report.
- Each PR's outcome is written to `status/<pr>.json` as soon as it is
  known; the report is made from those files.

---

## 5. Starting a run

### 5.1 Prepare

Pick a name and the PRs:

```bash
# every PR merged in a date range
.venv/bin/python cluster/prepare.py --name 2025-09 --since 2025-09-01 --until 2025-09-30

# or exactly these PRs (still through the hard filters)
.venv/bin/python cluster/prepare.py --name check --pr 65697 85079 99824

# these PRs, skipping the hard filters (to force a PR into triage)
.venv/bin/python cluster/prepare.py --name forced --pr 110975 --no-filter
```

It prints its progress and ends with a count of where the PRs stand:

```
[11:06:15] preparing /scratch/scholar/you/embedeval/runs/gold
[11:06:15] hard filters
[11:06:16] 19 PRs
[11:06:16] enriching, 4 at a time
[11:06:49] enriched 15/15 in 34 s
[11:06:49] dry runs, 8 at a time
[11:07:21] dry runs done in 32 s: 14 to triage
[11:07:22] commits: 42 present (ok)
[11:07:22] done in 66 s -> /scratch/scholar/you/embedeval/runs/gold/manifest.json
        1  dry run: no runnable pair
        4  hard filter
       14  to triage

next: python cluster/submit.py pipeline --run gold
```

A name can be used once; to start over, pick another name (or delete the
old run's folder yourself).

### 5.2 Submit

Look at the plan first, then submit:

```bash
.venv/bin/python cluster/submit.py pipeline --run 2025-09 --dry-run
.venv/bin/python cluster/submit.py pipeline --run 2025-09
```

```
run 2025-09: 15 PRs to triage, 0 to validate
  nodes: 2-4 (work needs 4; 20 idle; max 16)
  limit: 03:00:00; phase caps: triage 95 min, validation 75 min
  job script: /scratch/scholar/you/embedeval/runs/2025-09/jobs/pipeline-20261014-110728.sbatch
  forecast: sbatch: Job 475313 to start at 2026-10-14T11:25:30 ...
  submitted: job 475314; log /scratch/scholar/you/embedeval/runs/2025-09/jobs/job-475314.out
```

The node count follows the work: one node per 4 PRs to triage (or to
validate, if more), at most `nodes_max`. The job asks for a range,
`-N <min>-<wanted>`, so it starts on what is free rather than waiting for
every node it would like.

The job does not depend on your desktop: you can close it, and the job runs
on. It stops only when it finishes, at its time limit, or with `scancel`.

### 5.3 Follow it

```bash
squeue -u $USER                                         # running? on which nodes?
tail -f $SCRATCH/embedeval/runs/2025-09/jobs/job-<id>.out   # the phases
tail -f $SCRATCH/embedeval/runs/2025-09/logs/node0/worker-triage.log   # one node's PRs
.venv/bin/python cluster/report.py --run 2025-09         # the funnel so far
```

A node's worker log shows each PR as it finishes:

```
[11:07:33] node2 scholar-k062: filling the compile cache /scratch/.../shared/vllm-cache
[11:17:23] node2 scholar-k062: model server ready after 9.8 min
[11:24:53] node2 scholar-k062: triage 112110: reject (7.5 min)
[11:28:50] node2 scholar-k062: triage 85079: accept (11.4 min)
[11:29:10] node2 scholar-k062: model server stopped
```

To stop a job: `scancel <id>`. Nothing is lost: see section 8.

---

## 6. What a run produces

```
$SCRATCH/embedeval/runs/<name>/
├── manifest.json            the plan: every PR and where prepare left it, the code commit
│                            (and whether it had uncommitted changes), a copy of the config
├── report.md, report.json   the results (section 7)
├── status/<pr>.json         each PR's outcome, per phase: the single source of truth
│
├── candidates/              fetched PRs (cache/) and the hard filter's list
├── enriched/<pr>.json       the full record triage reads
├── dryrun/<pr>.json         every scenario/board pair: selected, or filtered and why
├── triage/<pr>.json         the verdict: accept/reject, the pair, problem statement, tests
├── triage/<pr>.trace.json   the model's whole conversation: reasoning, tool calls, timings
├── validate/                a verdict moved to another board for a retry (rare, 7.2)
├── instances/zephyr__zephyr-<pr>/
│   ├── instance.def         the image recipe
│   ├── instance.json        board, scenario, twister options
│   ├── run_tests.py         the test command inside the image
│   ├── test_patch.diff      the PR's test changes
│   ├── metadata.json        problem statement, commits, tests that must flip
│   └── validation.json      VALID or not, why, and the fingerprint of the recipe
│
├── logs/
│   ├── prepare/             enrich.log, fetch.log, dryrun/<pr>.log
│   └── node<N>/             worker-triage.log, worker-validate.log, server.log,
│                            triage-<pr>.log, validate-<pr>.log
├── jobs/                    each submit: pipeline-<time>.sbatch, config-<time>.json,
│                            job-<id>.out; submitted.jsonl lists them all
├── nodes/node<N>/           each node's scratch space (images while building, caches);
│                            empty of images once the job is done
└── lookup/                  CI images and SDK releases looked up during the run
```

Images are never kept: each is deleted once its instance is validated. The
recipe in `instances/` is enough to build it again (`build_instances.py`).

---

## 7. Reading the results

### 7.1 The report

`report.md` is made at the end of the job, and by `report.py` whenever you
ask. It starts with the funnel:

```
| Outcome                    | PRs |
|----------------------------|-----|
| hard filter                | 4   |
| dry run: no runnable pair  | 1   |
| triage: reject             | 6   |
| VALID                      | 8   |

Valid instances (8): 33690, 43405, 62109, 65697, 74435, 82272, 85079, 89534
```

then one row per PR: its outcome, a detail, the pair triage chose, and the
minutes spent in triage and validation.

### 7.2 Every outcome, and what to do

| Outcome | Meaning | What to do |
|---|---|---|
| `hard filter` | dropped by a hard filter; the detail says which (`revert`, `touches no tests/`, ...) | nothing, or `--pr ... --no-filter` to force it |
| `enrich failed` | GitHub or the clone could not give its record | see `logs/prepare/enrich.log`; prepare again |
| `dry run error` | twister could not run on it | see `logs/prepare/dryrun/<pr>.log` |
| `dry run: no runnable pair` | twister runs none of its scenarios on our boards | nothing: it cannot be an instance here |
| `triage: reject` | the model judged it unsuitable | read `triage/<pr>.json` -> `reason` if you doubt it |
| `triage: cap` | no verdict within the 30-min cap | rare; raise `caps.triage_pr` and resubmit if it matters |
| `triage: failed` | the triage step ended without a verdict | see `logs/node<N>/triage-<pr>.log` |
| `triage: server failed` | that node's model server never started | see `logs/node<N>/server.log`; **retried by a resubmit** |
| `triage: not yet` | not reached (the job ended or was cancelled first) | resubmit |
| `accepted, validation not yet` | accepted, not validated yet | resubmit |
| `validation: skipped` | not started, because of the disk guard | **retried by a resubmit** |
| `validation: failed` | generate or build failed, or a cap was hit | see `logs/node<N>/validate-<pr>.log` |
| `INVALID` | built, but the tests did not fail-then-pass; the detail says how | not an instance; nothing to promote |
| `VALID` | fails as built, passes with the fix | review it and promote it (section 9) |

The `INVALID` details:

- `tests PASSED before the fix -- the instance does not detect the bug`
- `tests still fail with the fix`, `the fix does not apply`
- `twister did not run (environment problem)` -- worth a look in the log;
  usually not the PR's fault.
- `nothing ran: the scenario is filtered or skipped on this board` --
  twister skipped the test at run time, for a reason the dry run cannot see
  (a Kconfig dependency, say). When this happens, validation tries once more
  on another board twister selects for the same scenario, QEMU boards first;
  the detail then reads e.g. `retried on qemu_x86: nothing ran on
  native_posix_64`. It is `INVALID` only if no other board works either.

### 7.3 Comparing with the gold set

`triage/gold.json` lists PRs whose right answer is known: 8 that make
valid instances and 11 that must be rejected (why each was chosen:
[stage1_pr_candidates.md](stage1_pr_candidates.md)). After a change to
triage or the pipeline, run them and check nothing moved:

```bash
.venv/bin/python cluster/prepare.py --name gold-<date> --pr $(.venv/bin/python -c \
    "import json; g=json.load(open('triage/gold.json')); print(*g['accept'], *g['reject'])")
.venv/bin/python cluster/submit.py pipeline --run gold-<date>
.venv/bin/python cluster/report.py --run gold-<date> --scorecard   # triage/scorecard.md
```

Expected: 4 hard filter, 1 dry run, 6 triage rejects, 8 VALID. The model
does not always choose the same board, so recipes can differ from the
committed ones while still being VALID.

---

## 8. Resuming, and when things go wrong

A job can end before the run is done: the time limit, `scancel`, a node
failure. **Submit again** with the same command:

```bash
.venv/bin/python cluster/submit.py pipeline --run 2025-09
```

It reads `status/` and does only what is left:

- a PR with an outcome is never done again (its files are not touched);
- a PR that was cut short has no outcome, so it is done again from the
  start of its step (a half-built image is cleaned up first);
- `triage: server failed` and `validation: skipped` are tried again;
- every other outcome is final.

When nothing is left, submit says so and points at the report.

Every node's work is in its own `nodes/node<N>/` folder and every step has
a cap, so one stuck PR or one bad node costs only its own PRs.

---

## 9. Reviewing and promoting

A `VALID` instance has passed the mechanical check. Before it goes into
the benchmark, a person should check what the model wrote:

1. **The problem statement**, in `instances/<id>/metadata.json`: is it an
   accurate, self-contained description of the bug or missing behaviour,
   without giving away the fix? It is what the agent will be given.
2. **The tests**, in `triage/<pr>.json` (`fail_to_pass`, `pass_to_pass`)
   and `instances/<id>/validation.json` (`before`, `after`): do the right
   tests fail before and pass after?
3. **The board and scenario** (`instance.json`): sensible for the change?
4. **Why it was accepted**: `triage/<pr>.json` -> `reason`; the full
   reasoning is in `triage/<pr>.trace.json`.

Then promote it -- this is the only step that writes into the repository:

```bash
R=$SCRATCH/embedeval/runs/2025-09
.venv/bin/python scripts/promote_instance.py --run-dir $R zephyr__zephyr-65697 zephyr__zephyr-85079
.venv/bin/python scripts/promote_instance.py --run-dir $R --all     # every VALID one in the run
```

```
  [ok  ] zephyr__zephyr-65697: promoted to apptainer/instances/zephyr__zephyr-65697
  [skip] zephyr__zephyr-43405: invalid: nothing ran: ...

  1/2 promoted
```

Promotion copies the recipe files and `validation.json` into
`apptainer/instances/<id>/`. It refuses an instance that is not VALID, or
whose recipe changed after it was validated (edit the problem statement, and
it must be validated again). Commit the new folders yourself.

A promoted instance is used like any other: build its image and run an
agent on it, as in the [README](../README.md#quick-start).

---

## 10. Where everything lives

On the network file systems, the same from every node:

```
~/code/EmbedEval/                       the code (git); one checkout serves every node
├── cluster/config.yaml                 your settings (not in git)
├── .venv/                              Python environment (not in git)
└── apptainer/instances/                the benchmark: promoted instances (in git)

/scratch/scholar/$USER/
├── hf/models/<model>/                  model weights                     (yours; read)
├── images/vllm-0.27.1.sif              vLLM server image                 (yours; read)
└── embedeval/                          scratch_root: everything the pipeline makes
    ├── shared/                         reused by every run; made by setup.py and prepare.py
    │   ├── zephyr.git                  full bare clone of Zephyr (~1 GB)
    │   ├── images/python-<py>-*.sif    base images, one per Python version
    │   ├── images/zephyr-sdk-*.sif     one per Zephyr SDK + Python pair (~1 GB each)
    │   ├── venvs/                      dry-run Python environments, one per Zephyr era
    │   ├── vllm-cache/                 the model server's compiled kernels
    │   ├── cache/                      Apptainer and uv download caches
    │   └── logs/                       how the shared images were built
    └── runs/<name>/                    one folder per run (section 6)
```

What you can delete:

| What | Safe to delete? |
|---|---|
| a folder in `runs/` | yes, when you no longer need its results (promote first) |
| `shared/vllm-cache/`, `shared/venvs/` | yes; rebuilt on the next run (about 8 min for the cache, a minute or two per venv) |
| `shared/images/` | yes; rebuilt when needed (minutes per image) |
| `shared/zephyr.git` | yes; `setup.py` clones it again |
| model weights, the vLLM image | only if you download them again |

Do not move `shared/vllm-cache/` or `shared/venvs/`: both contain their own
absolute path, so they break if moved. Delete them instead.

**Use `cluster/` (or `--run-dir`) for pipeline work.** The scripts in
`scripts/` still work on their own, but without `--run-dir` they write next
to the code (`candidates/`, `generated/`, `triage/runs/`) and into
`$SCRATCH/embedeval-twister`, as they did before run folders existed.
