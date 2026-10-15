# The Zephyr pipeline: how it works, and how to change it

This guide is for developers. It explains how the pieces of the pipeline
fit together, what each script does, how work is shared between nodes and
written to disk, how the Slurm job is built, and where to change things.

For using the pipeline -- setup, starting a run, reading the results,
promoting -- see [running_on_scholar.md](running_on_scholar.md) first.

---

## 1. The moving parts

```
 interactive Spark node                          spark-batch nodes, one Slurm job
 ──────────────────────                          ─────────────────────────────────────────────
 cluster/setup.py      shared/ folders,
                       clone, base images
 cluster/prepare.py ─┬─ scripts/filter_candidates.py   fetch, hard filter, enrich
                     ├─ scripts/dry_run.py              one process per PR, in parallel
                     ├─ scripts/apptainer_util.py       missing SDK images, built once
                     └─ scripts/zephyr_env.py           every commit fetched into the clone
                        └─► runs/<run>/manifest.json
 cluster/submit.py ──── cluster/status.py               what is left to do
                        └─► jobs/pipeline-<t>.sbatch ──► sbatch
                                                          │
                              templates/pipeline.sbatch   ▼
                                                     phase 1: srun ─► cluster/node_worker.py --phase triage
                                                          │            (one per node)
                                                          │  scripts/serve_triage_model.sh   vLLM server
                                                          │  scripts/triage_prs.py           one per PR
                                                          ▼
                                                     phase 2: srun ─► cluster/node_worker.py --phase validate
                                                          │  scripts/cleanup_instance.py
                                                          │  scripts/generate_instance.py
                                                          │  scripts/build_instances.py
                                                          │  scripts/validate_instance.py
                                                          ▼
                                                     phase 3: cluster/report.py
 cluster/report.py     any time
 scripts/promote_instance.py --run-dir <run>  ──►  apptainer/instances/ (the repository)
```

Two layers, kept apart on purpose:

- **`scripts/`** does the work for one PR or a few: each script is usable
  on its own, takes `--run-dir`, and knows nothing about Slurm.
- **`cluster/`** organises a run: what to do, on which node, with which
  limits, and what happened. It calls `scripts/` as subprocesses, never as
  imports for the actual work, so a step that hangs or crashes can be
  killed without taking the worker with it.

---

## 2. What each script does

### 2.1 `cluster/`

| Script | Runs on | Does | Writes |
|---|---|---|---|
| `config.py` | (library) | loads `config.yaml` over `config.example.yaml`; strict: an unknown key or a wrong type is an error; derives paths; `env_for()` gives the environment the scripts read | -- |
| `setup.py` | desktop | checks machine, Apptainer, `gh`, Slurm, weights, vLLM image; creates `shared/`, `runs/`; clones Zephyr; builds the base Python images. Never deletes | `shared/` |
| `prepare.py` | desktop | the PR list (`--since/--until` or `--pr`), hard filters, enrich, dry runs, missing SDK images, commits, manifest | `runs/<run>/` up to `manifest.json` |
| `submit.py` | desktop | what is left (from `status/`), node count, the job script, `sbatch` | `jobs/` |
| `node_worker.py` | each batch node | one node's share of one phase (section 4) | `status/`, `triage/`, `instances/`, `logs/node<N>/`, `nodes/node<N>/` |
| `status.py` | (library) | reads and writes `status/<pr>.json`; `triage_todo`, `validate_todo` | `status/` |
| `report.py` | anywhere | the funnel and per-PR table, from the manifest and `status/` only | `report.md`, `report.json` |
| `templates/pipeline.sbatch` | -- | the job script template | -- |

### 2.2 `scripts/`, the pipeline stages

| Script | Stage | In | Out (in the run folder) |
|---|---|---|---|
| `filter_candidates.py` | fetch, filter, enrich | GitHub API, the clone | `candidates/cache/`, `candidates/candidates.jsonl`, `enriched/<pr>.json` |
| `zephyr_env.py` | (library, used by enrich) | the PR's commits in the clone | the record's `twister` block: suites, scenarios, boards, Python, SDK, twister options |
| `dry_run.py` | dry run | the record; a minimal tree from `git archive`; twister `--dry-run` in the Python image | `dryrun/<pr>.json` |
| `triage_prs.py` | triage | the record, the dry run, `docs/pr_triage.md`, the model server | `triage/<pr>.json`, `<pr>.trace.json` |
| `generate_instance.py` | generate | the verdict, the record | `instances/<id>/` recipe files |
| `build_instances.py` | build | the recipe; shared Python and SDK images | `work/images/instances/<id>.sif` |
| `validate_instance.py` | validate | the image; the upstream fix from the clone | `instances/<id>/validation.json` |
| `cleanup_instance.py` | clean up | the instance id | removes its image, dry-run tree, overlays |
| `promote_instance.py` | promote | a VALID instance in the run | `apptainer/instances/<id>/` in the repository |
| `serve_triage_model.sh` | model server | a profile in `triage/models.json` | a vLLM server on `127.0.0.1:$PORT` |

Helpers: `layout.py` (where everything is read and written, section 5.1),
`apptainer_util.py` (image layers, builds, the only delete function),
`twister_pipeline.py` (the older single-node driver of generate to promote,
still usable for a few PRs). `build_bases.py`, `build_config.py` and
`instances.py` are the Docker side (NuttX, RIOT).

### 2.3 The images

```
python-<py>-<hash>.sif               Ubuntu 24.04, build tools, uv, Python <py>      shared/images/
  └─ zephyr-sdk-<sdk>-py<py>-<h>.sif   + the Zephyr SDK: host tools, compilers, QEMU   shared/images/
       └─ instances/<id>.sif             + Zephyr at the base commit, modules,         nodes/node<N>/work/images/
                                           requirements, the test patch, a pre-build   (deleted after validation)
```

Each is built `FROM` the one above (`Bootstrap: localimage`), from the
recipes in `templates/zephyr/apptainer/`. The hash in a name is a hash of
the recipe, so a changed recipe gives a new image instead of reusing a
stale one. The PR's era decides `<py>` and `<sdk>` (`zephyr_env.py`), so
the few Python and SDK images are shared by every PR of that era.

---

## 3. A run on disk, and who writes what

```
runs/<run>/
  manifest.json                 prepare.py, once
  candidates/ enriched/ dryrun/ prepare.py (through the scripts)
  lookup/                       zephyr_env.py, when a CI image or SDK is new to the tables
  jobs/                         submit.py, per submit
  status/<pr>.json              node_worker.py: the worker that owns the PR
  triage/<pr>.json, .trace.json triage_prs.py, run by that worker
  triage/manifest-<t>-<pid>.json one per triage call, with pr_triage-<t>-<pid>.md
  validate/<pr>-<board>.json    node_worker.py, for a board retry
  instances/<id>/               generate_instance.py, validate_instance.py
  logs/node<N>/                 that node's worker and its steps
  nodes/node<N>/work/           that node's images, overlays, temp files (EMBEDEVAL_WORK)
  nodes/node<N>/scr/            that node's model-server caches
  work/                         prepare's own work root (dry-run working files)
  report.md, report.json        report.py
```

**No two processes ever write the same file.** Every PR belongs to exactly
one worker in a phase, so its files have one writer. Node folders belong to
one node. Triage calls running side by side each write their own manifest.
Nothing is merged: the report is made by reading the per-PR files.

**Status files are written atomically**: to a temporary file in the same
folder, then renamed over the old one. A reader sees the old file or the new
one, never half of one. A stage appears in a status file only when it has
finished, one way or another; a PR with no entry for a stage is still to do.
That is all resuming needs.

What a status file looks like:

```json
{
  "pr": 43405,
  "triage":   {"state": "done", "verdict": "accept", "confidence": "high",
               "pair": "native_posix_64 / logging.log_backend_initialization",
               "minutes": 11.0, "node": 1},
  "validate": {"state": "done", "outcome": "VALID", "minutes": 36.0, "node": 0,
               "note": "retried on qemu_x86: nothing ran on native_posix_64"}
}
```

| Stage | States | Final? |
|---|---|---|
| `triage` | `done` (with a verdict), `cap`, `failed` | yes |
| | `server failed` | no: redone on a resubmit |
| `validate` | `done` (outcome `VALID` or `INVALID: <reason>`), `failed` | yes |
| | `skipped` (disk guard) | no: redone on a resubmit |

---

## 4. The Slurm job in detail

### 4.1 What `submit.py` decides

1. **What is left**: `status.triage_todo()` (reached triage, no triage
   outcome, or `server failed`) and `status.validate_todo()` (accepted, no
   validation outcome, or `skipped`).
2. **Nodes**: `work = max(ceil(triage_left / triage_per_node),
   ceil(validate_left / validate_per_node))`; `wanted = min(nodes_max,
   work)`; `minimum = min(nodes_min, wanted)`, lowered to the number of idle
   nodes (`sinfo`) if fewer are idle. The job is submitted with `-N
   minimum-wanted`: Slurm starts it on as many idle nodes as it can, up to
   `wanted`, as soon as `minimum` are free.
3. **Time**: refuses to submit unless `triage_phase + validate_phase + 5 <=
   time_limit` (the 5 minutes are for the report and slack).
4. **The job script**: `templates/pipeline.sbatch` filled in, written to
   `jobs/pipeline-<time>.sbatch`, with the whole config frozen beside it as
   `jobs/config-<time>.json`. The workers read the frozen copy, so editing
   `config.yaml` never changes a job already submitted.
5. `sbatch --test-only` for a start-time forecast, then `sbatch`; the job
   is logged in `jobs/submitted.jsonl`.

### 4.2 The job script

The generated script, with the default config:

```bash
#SBATCH -p spark-batch
#SBATCH -N 2-4                          # minimum-wanted
#SBATCH --ntasks-per-node=1             # one worker per node
#SBATCH --cpus-per-task=20              # all cores (Slurm's default is 1)
#SBATCH --gres=gpu:1
#SBATCH --mem=120500M                   # the whole node
#SBATCH -t 03:00:00
#SBATCH -o <run>/jobs/job-%j.out

timeout -k 2m 95m srun -N $NN -n $NN --ntasks-per-node=1 -c 20 --kill-on-bad-exit=0 \
    python cluster/node_worker.py --run <run> --config <frozen> --phase triage
timeout -k 2m 75m srun ... --phase validate
python cluster/report.py --run <run>
```

- `srun` returns when every node's worker has finished, so it is the
  **barrier between phases**: validation starts only when all triage is done.
- `timeout` around each `srun` makes the phase cap hard; the workers also
  stop starting new PRs near it (4.4), so the hard cap is a backstop.
- `--kill-on-bad-exit=0`: one node's worker failing does not kill the rest.
- The job never waits for a model server and never submits other jobs.

### 4.3 How a node finds its share

Each worker gets `SLURM_PROCID` (`N`, 0-based) and `SLURM_NTASKS` (`NN`)
from `srun`. It computes the PRs still to do in its phase, sorts them, and
takes every `NN`-th one starting at index `N`. Every worker computes the
same list from the same files, so they agree without talking to each other.

```
to do, sorted:  33690 43405 62109 65697 74435 82272 85079 89534     NN = 3
node0           33690             65697             85079
node1                 43405             74435             89534
node2                       62109             82272
```

On a resubmit the list is only what is left, split again over the new
job's nodes. A fixed split does not rebalance: a node whose PRs are quick
waits for the slowest node at the phase barrier.

### 4.4 Inside a worker

**Triage phase**:

1. Start the node's model server (`serve_triage_model.sh`) in its own
   process group, and wait for it, up to `caps.server_start`. If it never
   comes up, every PR of this node is marked `server failed` and the worker
   ends (no 30-minute wait per PR).
2. Run `triage_per_node` PRs at a time, each one a `triage_prs.py --run-dir
   <run> run --pr <pr>` subprocess, capped at `caps.triage_pr`. No new PR
   starts within 5 minutes of `caps.triage_phase`.
3. Stop the server, always (in a `finally`).

**Validation phase**:

1. Sweep this node's work folder: build temp folders and partial images a
   cancelled job left behind.
2. Run `validate_per_node` PRs at a time. For each:
   - skip it (`skipped`) if the run's instance images already exceed
     `disk_guard_gb`;
   - `cleanup_instance.py` (leftovers of a cut-short try), then
     `generate_instance.py`, `build_instances.py` (each image build capped
     at `caps.build`), `validate_instance.py`;
   - `cleanup_instance.py` again, always: the image and overlays go, the
     recipe and `validation.json` stay;
   - if the outcome is `nothing ran`, retry once on the next board twister
     selected for the scenario (QEMU first), if 10 minutes are left.
   No PR starts with under 10 minutes left of `caps.validate_phase`; each is
   capped at `caps.validate_pr`.

**Every step** runs in its own process group with its own cap. On a cap the
whole group is killed (SIGTERM, then SIGKILL after 20 s) and the step
returns 124. A build that times out is stopped with everything it started,
including Apptainer's detached `faked` process, and its temp folder removed.

**On SIGTERM** (`scancel`, or the job's time limit), the worker sets a
stopping flag, kills every running step's group and the server, and exits.
Steps cut short return 143 and nothing is recorded for them, so they have
no outcome and a resubmit redoes them. (They must not be recorded as
`failed`: that is final, and they would never be retried.)

### 4.5 The model server

`scripts/serve_triage_model.sh <profile>` runs `vllm serve` inside the vLLM
image, on `127.0.0.1:$PORT` only (nothing is exposed on the network). It
reads the profile from `triage/models.json`. Two things it does are needed
on GB10 and must not be removed:

1. **No `apptainer --nv`.** It injects the host's OpenGL/Vulkan libraries,
   and one of them crashes the container at start-up (`dl-tls.c: 618 ...
   Assertion 'listp != NULL' failed!`). The script binds only the four CUDA
   compute libraries into `/opt/hostnv` and runs with `--cleanenv`.
2. **`--safetensors-load-strategy eager`.** The default memory-maps the
   weights, and on GB10 copying memory-mapped data to the GPU stalls for
   minutes per tensor. Eager loading reads each file into RAM first.

It also uses `--gpu-memory-utilization 0.80` (the job's memory cap is 112
GiB, vLLM sees 120) and `--max-num-seqs 4`, which is why
`parallel.triage_per_node` is 4.

**The compile cache.** On its first start vLLM compiles kernels for the
model (about 8 minutes) and saves them in its cache; later starts reuse
them. The saved files contain the cache's absolute path, and vLLM writes
through it, so a cache cannot be copied or moved: a copy keeps writing into
the original. So every run uses one shared cache, `shared/vllm-cache`
(`paths.vllm_cache`), in place. The first server to start takes a lock
(`.fill.lock`) and fills it, then marks it `.warm`; servers starting
meanwhile compile into their node's own folder instead of touching a
half-filled cache. After that, every server starts from the warm cache.

Throughput, Qwen3.8 NVFP4 on one GB10: about 28 tokens/s for one request,
43 in total for 4 at once (11 each). Each PR is slower when sharing the
GPU, but the total is about twice as high. CPU cores do not matter; the
GPU's memory bandwidth (about 273 GB/s) is the limit.

---

## 5. Storage: where things are read and written

### 5.1 `--run-dir` and the environment

Every script in `scripts/` imports `layout.py` first. It takes `--run-dir
DIR` off the command line (or reads `EMBEDEVAL_RUN_DIR`) before any other
code computes a path. With it, everything goes into `DIR` (section 3);
without it, everything goes where it did before run folders existed:

| | with `--run-dir DIR` | without |
|---|---|---|
| fetched, enriched, dry runs | `DIR/candidates/`, `DIR/enriched/`, `DIR/dryrun/` | `candidates/...` in the repo |
| verdicts | `DIR/triage/` | `triage/runs/<time>_<profile>/` in the repo |
| recipes | `DIR/instances/` | `generated/` in the repo |
| images, overlays, temp | `DIR/work/` | `$SCRATCH/embedeval-twister/` |
| agent outputs | `DIR/outputs/` | `outputs/` in the repo |

The variables `cluster/` sets for every step (`config.env_for()`):

| Variable | Means | Set from |
|---|---|---|
| `EMBEDEVAL_RUN_DIR` | the run folder | the run |
| `EMBEDEVAL_WORK` | this process's images, overlays, temp files; overrides `DIR/work` | `nodes/node<N>/work` in a worker |
| `EMBEDEVAL_SHARED` | Python and SDK images, venvs, uv and Apptainer caches | `paths.shared` |
| `EMBEDEVAL_ZEPHYR_CLONE` | the full bare clone | `paths.zephyr_clone` |
| `EMBEDEVAL_MODELS` | the model weights folder | `paths.models` |
| `EMBEDEVAL_VLLM_IMAGE` | the vLLM `.sif` | `paths.vllm_image` |
| `EMBEDEVAL_VLLM_CACHE` | the compile cache the server uses | `paths.vllm_cache`, or the node's own while it is being filled |
| `EMBEDEVAL_SCRATCH` | where the server keeps its other caches | `nodes/node<N>/scr` |
| `EMBEDEVAL_BUILD_TIMEOUT_MIN` | the cap of one image build | `caps.build` |

### 5.2 Shared versus per run

| | Where | Made by | Shared by |
|---|---|---|---|
| Zephyr clone | `shared/zephyr.git` | `setup.py`; commits fetched by `prepare.py` | every run, read-only during jobs |
| Python, SDK images | `shared/images/` | `setup.py`, `prepare.py` | every run |
| dry-run venvs | `shared/venvs/` | `dry_run.py`, under a lock per venv | every run |
| vLLM compile cache | `shared/vllm-cache/` | the first server, under a lock | every server |
| instance images | `nodes/node<N>/work/images/instances/` | `build_instances.py` | nobody: deleted after validation |
| everything else | `runs/<run>/` | the run | nobody |

Two rules keep parallel work safe:

- **Workers never build shared things.** `prepare.py` builds every SDK
  image the run needs and fetches every commit the workers will read,
  before the job. Two nodes never build the same image or fetch into the
  clone at once.
- **Workers never delete outside their node's folder.** All deletion goes
  through `apptainer_util.remove()`, which refuses any path outside the
  work root -- and a worker's work root is `nodes/node<N>/work`.

---

## 6. Commands for developers

### 6.1 One stage on its own

Every stage can be run by hand into a run folder:

```bash
R=$SCRATCH/embedeval/runs/dev
export EMBEDEVAL_SHARED=$SCRATCH/embedeval/shared EMBEDEVAL_ZEPHYR_CLONE=$SCRATCH/embedeval/shared/zephyr.git

python scripts/filter_candidates.py --run-dir $R fetch --pr 65697
python scripts/filter_candidates.py --run-dir $R enrich --pr 65697
python scripts/dry_run.py --run-dir $R 65697
python scripts/triage_prs.py --run-dir $R run --profile qwen38-nvfp4 --pr 65697   # needs a server
python scripts/generate_instance.py --run-dir $R --pr 65697 --triage $R/triage/65697.json
python scripts/build_instances.py --run-dir $R --instance zephyr__zephyr-65697    # on a batch node
python scripts/validate_instance.py --run-dir $R zephyr__zephyr-65697
python scripts/cleanup_instance.py --run-dir $R zephyr__zephyr-65697
```

Builds and validation need a node where Apptainer can build: a
`spark-batch` allocation (`salloc -p spark-batch -N 1 -c 20 --mem=120500M
--gres=gpu:1 -t 2:00:00`). Remember the 2-job limit.

### 6.2 A model server by hand

```bash
sbatch scripts/serve_triage_model.sh qwen38-nvfp4      # -> triage-server-<jobid>.log in the repo
squeue -u $USER                                         # its node
srun --jobid=<id> --overlap curl -s 127.0.0.1:8000/v1/models     # ready when this answers
srun --jobid=<id> --overlap -c 4 .venv/bin/python scripts/triage_prs.py --run-dir $R \
     run --profile qwen38-nvfp4 --pr 65697 --parallel 4
```

`srun --jobid=<id> --overlap` runs a command inside a job you already have,
which is how you reach a server bound to `127.0.0.1` on its node.

### 6.3 A worker phase by hand

To debug a phase on one node, inside an allocation:

```bash
SLURM_PROCID=0 SLURM_NTASKS=1 .venv/bin/python cluster/node_worker.py \
    --run $R --config $R/jobs/config-<time>.json --phase validate
```

It takes the whole share (one node of one), logs to `logs/node0/`, and
writes statuses as in a job.

### 6.4 Useful flags

| Command | Flag | Does |
|---|---|---|
| `prepare.py` | `--pr ... --no-filter` | skip the hard filters |
| `submit.py` | `--dry-run` | write the job script, print the plan, submit nothing |
| `submit.py` | `--idle N` | plan as if N nodes were idle |
| `report.py` | `--scorecard` | also score the verdicts against `triage/gold.json` |
| any script | `--config FILE` | another config instead of `cluster/config.yaml` (`cluster/`) |
| any `scripts/` | `--run-dir DIR` | write into `DIR` |
| `dry_run.py` | `--keep-tree` | keep the exported tree, to look at it |
| `filter_candidates.py` | `selftest` | check the hard filters keep all 8 known-good PRs |
| `promote_instance.py` | `--all` | every VALID instance |

---

## 7. Changing things

### 7.1 The config

`cluster/config.yaml` overrides `cluster/config.example.yaml` key by key.
Unknown keys and wrong types are refused, so a typo is an error, not a
silently ignored setting. `${USER}` and `${SCRATCH}` are expanded.

| Key | Default | Notes |
|---|---|---|
| `scratch_root` | `/scratch/scholar/${USER}/embedeval` | holds `shared/` and `runs/` |
| `paths.shared` | `null` = `<scratch_root>/shared` | point at another user's or an older shared folder to reuse its images |
| `paths.zephyr_clone` | `null` = `<shared>/zephyr.git` | must be a **full** clone (not blobless: triage greps the tree) |
| `paths.models` | `$SCRATCH/hf/models` | one folder per model, named as in `triage/models.json` |
| `paths.vllm_image` | `$SCRATCH/images/vllm-0.27.1.sif` | |
| `paths.vllm_cache` | `null` = `<shared>/vllm-cache` | never a cache used outside these runs (4.5) |
| `python_versions` | `3.9`, `3.10`, `3.12` | base images `setup.py` builds; others are built when a PR needs one |
| `slurm.partition` | `spark-batch` | |
| `slurm.nodes_min`, `nodes_max` | 2, 16 | the range of 4.1 |
| `slurm.time_limit` | `03:00:00` | at most 4 h on Scholar |
| `slurm.cpus_per_node`, `mem`, `gres` | 20, `120500M`, `gpu:1` | a whole Spark node |
| `model.profile` | `qwen38-nvfp4` | an entry of `triage/models.json` |
| `caps.server_start` | 30 | min until the server must answer |
| `caps.triage_pr` | 30 | min per PR's triage |
| `caps.triage_phase` | 95 | min for all of triage |
| `caps.build` | 45 | min per image build |
| `caps.validate_pr` | 70 | min per PR: generate, build, validate (a retry included) |
| `caps.validate_phase` | 75 | min for all of validation |
| `parallel.enrich`, `dry_run` | 4, 8 | on the desktop, in `prepare.py` |
| `parallel.triage_per_node` | 4 | matches the server's `--max-num-seqs 4` |
| `parallel.validate_per_node` | 4 | builds use all 20 cores between them |
| `disk_guard_gb` | 250 | no new build while the run's images exceed this |

Keep `triage_phase + validate_phase + 5 <= time_limit`; `submit.py`
checks. To give one phase more time, take it from the other.

To use different settings for one run only, keep a second file and pass it
everywhere: `--config cluster/big-run.yaml` to `prepare.py`, `submit.py`
and `report.py`.

### 7.2 The job script

`cluster/templates/pipeline.sbatch` is a Python format string (`{name}`,
`{nodes}`, ...; literal braces doubled). Edit it to add `#SBATCH` lines (an
account, `--mail-user`), module loads or a different phase order. A job
already submitted keeps the script it was given; resubmit to use the new
one. Do not edit `jobs/pipeline-*.sbatch` by hand: `submit.py` writes a new
one on every submit.

### 7.3 Another model

1. Download the weights to `paths.models`.
2. Add a profile to `triage/models.json`: copy one, and take the tool-call
   parser, reasoning parser and sampling settings from the model card or
   the vLLM recipe. Check the parser names exist: `vllm serve --help=all |
   grep -A6 tool-call-parser`.
3. Start it by hand (6.2); make one chat call and one tool call.
4. Set `model.profile`, then run the gold set
   ([running_on_scholar.md](running_on_scholar.md), 7.3).

Generation speed on GB10 is set by the bytes of weights read per token.
Models tried:

| Model | Weights | Decode | Result |
|---|---|---|---|
| **Qwen3.8-27B NVFP4** | 25 GB | ~28 tok/s | used; matches the gold set |
| Qwen3.8-27B FP8 | 29 GB | ~12 tok/s | same judgement, 2.3 times slower |
| Nemotron-3-Super-120B-A12B NVFP4 | 80 GB | ~26 tok/s | misread the task on its one test; 16-min start |
| models over ~110 GB | -- | -- | do not fit one Spark node |

Profile flags that matter: `--enable-prefix-caching` (reuses the prompt
across tool rounds: 15 s to 1.6 s on a 23K-token prompt),
`--language-model-only` (skips a vision tower), `--speculative-config` with
MTP (if the model has an MTP head), `--reasoning-parser`, and
`--enable-auto-tool-choice --tool-call-parser` (without the right parser,
tool calls come back as plain text).

### 7.4 Triage itself

| What | Where |
|---|---|
| the instructions the model gets | `docs/pr_triage.md` (read at run time; a copy is saved per call) |
| how the record is rendered | `render_record` in `scripts/triage_prs.py` |
| the record's size budget | `RECORD_BUDGET` (400,000 characters; larger records are trimmed, `fit_record`) |
| tool rounds before it must answer | `MAX_TOOL_ROUNDS` (8) |
| times a bad verdict is sent back | `MAX_REPAIRS` (2) |
| what a verdict must satisfy | `check_verdict`: required fields, a pair the dry run selected, test names that occur in the suite |
| the gold set | `triage/gold.json` |

Change one thing at a time and rerun the gold set, so you know what a
change did. Read the traces (`<pr>.trace.json`) before tuning: on this
hardware the time is almost all generated tokens, mostly hidden reasoning,
not tools or prompt reading.

### 7.5 The hard filters

`classify()` in `scripts/filter_candidates.py`. The rule: a filter belongs
there only if it cannot reject any of the eight known-good PRs, because a
PR rejected there never reaches triage. After a change, run
`filter_candidates.py selftest`.

### 7.6 Boards

A board is usable when twister can run it on ARM64 with no hardware, and
the SDK image has its toolchain and emulator. To add one:

1. Add its names (all spellings across Zephyr's history) to
   `CANDIDATE_BOARDS` in `scripts/zephyr_env.py`. The dry run then tries it.
2. Make sure `templates/zephyr/apptainer/sdk.def` installs its toolchain
   and QEMU. A changed recipe gives new SDK images (new hash).
3. Describe it in the "What we can run" section of `docs/pr_triage.md`.
4. Run the gold set.

### 7.7 Another project

The method is not specific to Zephyr; the facts are:

| Piece | Zephyr | Another project |
|---|---|---|
| hard filters, enrichment | `filter_candidates.py`, `zephyr_env.py` | how your tests are found and declared; your targets' facts |
| dry run | twister's filters | your test runner's own selection, if it has one |
| record rendering, verdict checks | `render_record`, `check_verdict` | your record's sections; your test-name syntax |
| instructions | `docs/pr_triage.md` | written for your project |
| gold set | `triage/gold.json` | your known instances and rejects |
| image recipes, `run_tests` | `templates/zephyr/apptainer/` | yours |

Unchanged: `cluster/`, `serve_triage_model.sh`, `models.json`, and the
run, trace and scoring machinery of `triage_prs.py`.

---

## 8. Known limits

- **The dry run cannot see every filter.** Twister evaluates some
  conditions (a `filter:` expression on Kconfig, say) only when it builds.
  So a pair the dry run selected can still run nothing. Validation retries
  once on another board (4.4); a PR can still end `INVALID: nothing ran`.
- **The model is not deterministic.** The same PR can get a different board,
  problem statement or, rarely, verdict on another run. Review before
  promoting.
- **The split is fixed.** Nodes do not take work from each other; the
  slowest node sets the phase's length. A shared work queue is a possible
  later change.
- **One job at a time per account**, with a desktop open (the 2-job limit).
- **Images are not kept.** Every instance image is rebuilt from its recipe
  when needed (10-20 min). A container registry would avoid that; not done.
- **Scholar-specific.** Partition, node shape, and the vLLM workarounds are
  Scholar's. The config covers the first two; the third is in
  `serve_triage_model.sh`.

---

## 9. Problems you may meet

| Symptom | Cause, fix |
|---|---|
| vLLM dies at once with `dl-tls.c: 618` | `--nv` used; the serve script must bind the compute libraries instead (4.5) |
| server stuck at "Loading safetensors ... 0%" | memory-mapped loading; `--safetensors-load-strategy eager` (4.5) |
| out of memory on the desktop | `spark-interactive` caps jobs at 60 GB; models run on `spark-batch` |
| a 10-minute request silently runs three times | LiteLLM's 600 s default timeout retries; `triage_prs.py` sets `timeout=3600`, no retries |
| a job or step much slower than expected | Slurm grants 1 CPU unless asked: `--cpus-per-task=20`, `srun -c` |
| GitHub "rate limit exceeded" | `gh auth login` |
| `setup.py`: Zephyr clone `.partial` left | an interrupted clone; delete that folder and rerun |
| `pkill -f <pattern>` kills your own shell | the pattern is in your own command line; kill by PID |
| `git push` asks for credentials | `git -c credential.helper='!gh auth git-credential' push ...` |
| a run writes into the repository or `$SCRATCH/embedeval-twister` | a script was run without `--run-dir`; use `cluster/` or pass it |
