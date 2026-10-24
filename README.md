# EmbedEval

A benchmark for evaluating LLM coding agents on **embedded RTOS bug fixes**.

Each task is a real merged pull request from Zephyr, NuttX, or RIOT. The agent
gets the repository at the commit *before* the fix, plus the tests the PR added.
It has to make those tests pass. Everything runs inside a container with a real
cross-compiler toolchain, so a patch only counts if the firmware actually builds
and the tests actually run.

- **Zephyr** instances are built, validated and run with Zephyr's own test
  runner, **twister**, in **Apptainer** images (no Docker needed), and are
  generated from PRs by a pipeline — see [Zephyr: the twister pipeline](#zephyr-the-twister-pipeline).
  On Purdue Scholar the pipeline runs as one Slurm job over many nodes:
  [docs/running_on_scholar.md](docs/running_on_scholar.md) (how to use it) and
  [docs/pipeline_internals.md](docs/pipeline_internals.md) (how it works).
- **NuttX** instances pair a `nuttx-apps` ostest change with the kernel fix it
  checks, found by model triage and proven by validation — see
  [docs/nuttx_pr_workflow.md](docs/nuttx_pr_workflow.md). The 30 from the
  pipeline are **Apptainer** recipes in `apptainer/instances/nuttx/` (see
  [NuttX in Apptainer](#nuttx-in-apptainer)); 7 older ones are **Docker**
  images in `docker/instances/`.
- **RIOT** instances are **Docker** images in `docker/instances/`.

**Instances:** 18 Zephyr (validated, in `apptainer/instances/`), 37 NuttX (30
Apptainer ones in `apptainer/instances/nuttx/`, 7 Docker ones), 3 RIOT.

---

## Requirements

| | |
|---|---|
| Python | 3.11 (tested) |
| Docker | NuttX and RIOT: running, ~40 GB free for images |
| Apptainer | Zephyr: 1.2+ with rootless `--fakeroot` builds; ~9 GB of shared images plus 2–5 GB per instance while it is built. With `cluster/`, all under `$SCRATCH/embedeval/`; scripts run on their own use `$EMBEDEVAL_WORK` (default `$SCRATCH/embedeval-twister`) |
| git | for cloning upstream repos during validation |
| API key | for whichever model you run |

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Put your API key in a `.env` file at the repo root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Recognised keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`TOGETHER_API_KEY`. The runner checks for the right one before starting and
exits early if it is missing, rather than failing partway through a run.

Note that mini-swe-agent *also* loads a global config of its own from outside
the repo — on macOS, `~/Library/Application Support/mini-swe-agent/.env`. It
prints the path at startup. If a key seems to work without being in your `.env`,
or a stale key keeps being used, that file is why.

Activate the venv before running anything. The commands below assume it is
active; a system Python will not have `minisweagent` and will fail only once a
run actually starts, not at `--dry-run`.

Images are large — a NuttX instance is ~8.7 GB — and each Docker project needs
its base image built once before any of its instances. Zephyr's shared images
are built automatically the first time an instance of their era needs them.

---

## Two halves of the project

These are deliberately separate and share no code:

- **`scripts/`** — *building* instances. Turns a PR into an image (Apptainer for
  Zephyr, Docker for NuttX and RIOT) and proves the image behaves correctly.
- **`harness/`** — *running* agents. Points a model at an image and grades what
  it produces.

You only need `scripts/` when adding or repairing instances. Day to day, you use
`harness/`.

For NuttX PR discovery and instance generation, see
[`docs/nuttx_pr_workflow.md`](docs/nuttx_pr_workflow.md).

---

## Quick start

Run one agent on one instance and grade the result. NuttX (Docker):

```bash
# 1. build the images (once per project, slow)
python scripts/build_bases.py --repo nuttx
python scripts/build_instances.py --instance nuttx__nuttx-11889

# 2. run the agent
python harness/run.py --instance nuttx__nuttx-11889 --model claude-opus-4-6 -v

# 3. grade the patch it produced
python harness/evaluate_patches.py --instance nuttx__nuttx-11889 --model claude-opus-4-6
```

Zephyr (Apptainer): the same three steps. The instances are read from
`apptainer/instances/`, and the image is built from the recipe there:

```bash
python scripts/build_instances.py --instance zephyr__zephyr-65697
python harness/run.py --instance zephyr__zephyr-65697 --model claude-opus-4-6 -v
python harness/evaluate_patches.py --instance zephyr__zephyr-65697 --model claude-opus-4-6
```

`-v` streams the agent's reasoning, commands, and output as it works. Without it
the run is silent until it finishes.

---

## Selecting instances

Every command takes the same three selection flags:

```bash
--instance zephyr__zephyr-65697    # one or more, space separated
--repo nuttx                       # every instance in one project
--all                              # all 58
```

Instance ids are `<project>__<project>-<PR>`, matching the directory names under
`apptainer/instances/` (Zephyr), `apptainer/instances/nuttx/` (NuttX, Apptainer;
ids `nuttx__nuttx-<kernel PR>-a<apps PR>`) and `docker/instances/` (NuttX, RIOT). Use
lowercase. `EMBEDEVAL_INSTANCES_DIR` points every command at another folder
instead -- `generated/`, say, to work on instances before they are promoted.

## Selecting models

Use the short name or the full LiteLLM name:

| short | full |
|---|---|
| `claude-opus-4-6` | `anthropic/claude-opus-4-6` |
| `gpt-5.4` | `openai/gpt-5.4` |
| `gemini-2.5-pro` | `gemini/gemini-2.5-pro` |
| `Qwen-3-coder` | `together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8` |
| `DeepSeek-V3` | `together_ai/deepseek-ai/DeepSeek-V3` |
| `qwen38-nvfp4` | `openai/qwen38-nvfp4` — local, served by `scripts/serve_triage_model.sh`; run the harness on the server's node |

Add `--dry-run` to `run.py`, `evaluate_patches.py`, `build_instances.py`, or
`build_bases.py` to see what each *would* do without starting containers or
spending money.

---

## Where results go

```
outputs/<project>/<model>/<PR>/
    patch.diff          the agent's changes
    trajectory.json     every message, command, and result
outputs/results.json    grades from the last evaluate run
```

---

## How an instance works

A NuttX or RIOT directory under `docker/instances/` holds four things:

| file | purpose |
|---|---|
| `Dockerfile` | clones the repo at the pre-fix commit, configures, pre-builds |
| `test_patch.diff` | the tests the PR added, applied without the fix |
| `run_tests.sh` | installed as `run_tests`; exits 0 pass, 1 fail, 2 timeout |
| `metadata.json` | commits, problem statement, expected failing tests |

A Zephyr instance under `apptainer/instances/` holds an Apptainer recipe
instead: `instance.def`, `instance.json` (board, scenario, twister options),
`run_tests.py` (runs twister for that one scenario and reports every test),
`test_patch.diff`, `metadata.json`, and `validation.json` -- the record that
it fails before the fix and passes after, with a fingerprint of the recipe it
validated. Only `scripts/promote_instance.py` writes there, and only for a
valid instance whose recipe still matches that fingerprint.

The agent is given the problem statement and a shell. It never sees the real
fix. Test files are protected — the runner diffs against `protected_paths` and
warns if the agent edited them, so it cannot pass by rewriting the test.

Grading happens in a **fresh container** (for Zephyr, a fresh overlay on the
image), not the one the agent worked in. Only
`patch.diff` crosses over, so nothing the agent left behind — a stale build
artifact, a running process, a modified test — can influence the result.

---

## Validating an instance

An instance is only useful if it **fails before the fix and passes after**.
`validate_instance.py` checks exactly that: it runs the tests on the base
commit, expects failure, applies the real upstream fix, and expects a pass.

```bash
python scripts/validate_instance.py nuttx__nuttx-11889 --verbose
python scripts/validate_instance.py zephyr__zephyr-65697
```

Run this on any instance you build or change. An instance that passes step 1 is
broken — it means the test cannot detect the bug, and every agent will score a
free pass on it. This is not theoretical; see the `CONFIG_NDEBUG` note in
`docker/instances/nuttx__nuttx-11889/Dockerfile` for a case where it happened.

---

## Zephyr: the twister pipeline

Zephyr instances are generated from merged PRs, never written by hand. On
Scholar, run the whole pipeline with `cluster/` -- prepare a run, submit one
Slurm job, review the report, promote -- as described in
[docs/running_on_scholar.md](docs/running_on_scholar.md). The stages, run one
at a time (each script also takes `--run-dir DIR` to write into a run folder):

```bash
python scripts/filter_candidates.py fetch --since 2024-01-01 --until 2024-06-30
python scripts/filter_candidates.py filter          # hard filters
python scripts/filter_candidates.py enrich          # GitHub context + twister facts
python scripts/dry_run.py <pr>...                   # which scenario/board pairs can run
python scripts/triage_prs.py run --profile qwen38-nvfp4 --pr <pr>...   # model verdicts
python scripts/twister_pipeline.py --pr <pr>... --verdicts <dir>       # generate, build,
                                                    #   validate, promote, clean up
```

- **enrich** adds a `twister` block read from the PR's own commits: the
  suites and scenarios it changes, the board names valid at its base, and the
  Python, SDK and twister options of its era (`scripts/zephyr_env.py`).
- **dry run** asks twister's own filters which pairs can run, in seconds,
  with no SDK or modules. A PR with no runnable pair is rejected before triage.
- **triage** (`docs/pr_triage.md`) picks one selected pair, writes the problem
  statement and the test lists.
- **generate → build → validate** turns a verdict into an Apptainer image
  layered on shared Python and SDK images for its era, then checks that
  `run_tests` fails as built and passes with the upstream fix. Work happens in
  `generated/` (not in git).
- **promote** copies a valid instance to `apptainer/instances/`, the committed
  benchmark that evaluation runs on.

Builds and runs need a node where Apptainer can build; on Scholar, inside a
`spark-batch` job. How the stages, the model server and the cluster job fit
together: [docs/pipeline_internals.md](docs/pipeline_internals.md).

---

## NuttX in Apptainer

The same NuttX instance can be an Apptainer recipe instead of a Dockerfile,
for hosts with no Docker -- on Scholar, the ARM64 Spark nodes, where sim:nsh
builds natively. Render it with `--runtime apptainer`; the recipe goes to
`generated/` and its metadata says `"runtime": "apptainer"`, which is what
every tool below checks:

```bash
python scripts/render_nuttx_instance.py --runtime apptainer \
    --metadata docker/instances/nuttx__nuttx-11889/metadata.json \
    --test-patch docker/instances/nuttx__nuttx-11889/test_patch.diff

# inside a spark-batch job; local clones instead of GitHub, if you have them
export EMBEDEVAL_INSTANCES_DIR=generated
export EMBEDEVAL_NUTTX_CLONE=/scratch/scholar/$USER/repos/nuttx.git
export EMBEDEVAL_NUTTX_APPS_CLONE=/scratch/scholar/$USER/repos/nuttx-apps.git
python scripts/build_instances.py --instance nuttx__nuttx-11889
python scripts/validate_instance.py nuttx__nuttx-11889
python harness/run.py --instance nuttx__nuttx-11889 --model <model>
python harness/evaluate_patches.py --instance nuttx__nuttx-11889 --model <model>
```

- **Images** (`templates/nuttx/apptainer/`): `base.def` is the shared layer
  (Ubuntu 24.04 and sim:nsh's tools, built once, named by a hash of its
  recipe); `instance.def` adds the kernel and apps at their base commits,
  configured exactly as the Dockerfile does, with the test patch. Both go
  under the work root (`scripts/apptainer_util.py`), as Zephyr's do.
- **Validation** runs the Docker validator's NuttX steps in one overlay and
  writes `validation.json`. Grading builds, then runs `run_tests`, in a fresh
  overlay.
- **apps/** is kept out of the kernel's git in the image, so the agent's patch
  holds kernel changes only. (Under Docker, `git add -A` picks up the nested
  apps repository as a submodule entry.)
- **Old kernels on ARM64.** sim on an ARM64 host needs a kernel from
  2022-02-02 on. Kernels from 2023-07-17 to 2023-11-17 lack the ARM64 table in
  `sim_tcbinfo.c` and do not build; the image backports it from upstream
  (`sim_tcbinfo_arm64.patch`) and commits it before the agent starts, so it is
  never part of a patch.
- **Not every instance carries over to ARM64**; validate each one there.
  Of the seven, 11889, 11898, 12802 and 9923 are valid on Scholar's nodes.
  14152's fix is in the x86 setjmp code, so on ARM64 its test passes before
  the fix. 8832 and 8885 fail correctly at their base, but with the fix ostest
  still dies (SIGBUS) later, in its vfork() test: the sim's ARM64 vfork crashes
  on kernels from before the 2023-07 fork rework (c33d1c9c97).
- **Speed.** Builds in an overlay on `/scratch` are slower than in a plain
  directory: a full sim:nsh build takes about two minutes, an ostest run
  about two.

---

## Layout

```
harness/            run.py, evaluate_patches.py, paths.py, projects.py,
                    apptainer_env.py (Zephyr agent environment)
cluster/            the Zephyr pipeline on Scholar: config, setup.py, prepare.py,
                    submit.py, node_worker.py, report.py, the job template
docs/               running_on_scholar.md (using the pipeline),
                    pipeline_internals.md (how it works), pr_triage.md (the
                    model's triage instructions), stage1_pr_candidates.md (the gold PRs)
triage/             models.json (model profiles), gold.json (the Zephyr gold set),
                    NuttX: nuttx_apps_gold.json, ga/ (prompt tuning), nuttx_reviewed/
scripts/            Zephyr pipeline: filter_candidates.py, zephyr_env.py,
                    dry_run.py, triage_prs.py, generate_instance.py,
                    build_instances.py, validate_instance.py, twister_pipeline.py, ...
                    Docker side: build_bases.py, build_config.py
templates/zephyr/   Apptainer recipes, run_tests, lookup tables
docker/bases/       Docker base images (NuttX, RIOT)
docker/instances/   NuttX and RIOT instances; the hand-built Zephyr ones are
                    kept as reference metadata only and are not built
apptainer/instances/  validated Zephyr instances: the benchmark (recipes; images
                    are built from them); nuttx/ holds the validated NuttX ones
generated/          Zephyr instances being generated and validated (not in git)
outputs/            agent runs and grades
archive/            superseded work, kept for provenance
```

Two files carry the per-project differences, and they are the first place to
look when something behaves differently across RTOSes:

- **`harness/projects.py`** — prompts, build and test commands, protected paths,
  cleanup behaviour. Instance `metadata.json` overrides these per instance.
- **`scripts/build_config.py`** — Docker base images, build arguments,
  platform (NuttX and RIOT).
- **`templates/zephyr/apptainer/`** — the Zephyr image recipes and `run_tests`.

Adding a fourth RTOS means adding an entry to each, not writing a new runner.
