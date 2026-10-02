# Model-based PR triage on Scholar's DGX Spark nodes — setup guide

How to host an open-weights model on Purdue Scholar's DGX Spark nodes and use
it to triage pull requests. Written from setting this up for Zephyr in
EmbedEval (branch `feature/pr-filtering`); everything here was done and
measured on Scholar between 2026-09-27 and 2026-10-01.

Part 1 is the infrastructure, which is the same for every project. Part 2 is
the triage method and what to change for another project such as NuttX.
Part 3 is gotchas.

Paths use `$SCRATCH` for `/scratch/scholar/$USER`.

---

# Part 1 — Infrastructure

## 1.1 The shape of it

```
 /home/$USER (code, venv, gh login)      /scratch/scholar/$USER (weights, container image, logs)
                 \                         /
                  network drives, visible from every node
                 /                         \
 ┌───────────────────────────┐      ┌──────────────────────────────────────────┐
 │ OnDemand desktop          │      │ spark-batch node (sbatch, 120 GB)        │
 │ spark-interactive, 60 GB  │      │                                          │
 │                           │      │  apptainer → vLLM server on 127.0.0.1    │
 │ edit code, submit jobs,   │ srun │       ▲ HTTP (OpenAI API format)         │
 │ attach with srun --overlap├─────►│  triage script (LiteLLM client)          │
 └───────────────────────────┘      │   reads records + local git clone        │
                                    │   writes triage/runs/<run>/              │
                                    └──────────────────────────────────────────┘
```

- **Model weights** are just files on scratch.
- **vLLM** is the inference server. It loads the weights onto the GPU and
  serves an OpenAI-compatible HTTP API. It runs inside NVIDIA/vLLM's container
  through **Apptainer**, because there is no Docker on Scholar and installing
  vLLM by hand on this ARM GPU is fragile.
- **LiteLLM** is only a client library. It lets the same script call the local
  model or a hosted one (Claude, GPT) by changing a model string. The plain
  `openai` package would also work.
- The **triage script** runs on the same batch node and talks to
  `127.0.0.1:8000`. Nothing is exposed on the cluster network.

## 1.2 Scholar Spark facts that shape everything

Checked with `scontrol show partition`, `sacctmgr` and test jobs.

| | `spark-interactive` | `spark-batch` |
|---|---|---|
| Nodes | 76 | 20 (`scholar-k060`–`k079`) |
| Memory per job | **max 60 GB** (hard cap) | **120.5 GB** |
| How | OnDemand desktop | `sbatch` / `salloc` |

- Every node is a GB10: one Blackwell GPU and 20 Arm cores, with ~120 GB
  **shared** between CPU and GPU. The job's memory cap covers the model too.
  **Anything over ~50 GB of weights needs `spark-batch`.**
- The account allows **2 running jobs** at once, each with a **4-hour maximum**.
  An OnDemand desktop plus one model server uses both slots.
- `sbatch` grants **1 CPU** unless you pass `--cpus-per-task=20`. An `srun`
  step inside a job also gets 1 CPU unless you pass `-c`.
- Storage:
  - home: 25 GB quota, permanent — code and venv;
  - `$SCRATCH`: 1 TB — weights, images, logs;
  - `/tmp`: node-local and deleted when the job ends.
- **No Docker, and no root.** Apptainer is installed. `/scratch` and home are
  mounted into containers automatically.
- Batch nodes reach Hugging Face, GitHub and Docker Hub.
- Unauthenticated GitHub API calls are exhausted for the shared IP, so you need
  a GitHub login (`gh`).
- Slurm puts `salloc` shells on the compute node. `srun --jobid=<id> --overlap`
  runs a command inside an existing job, from your desktop.

## 1.3 One-time setup

### GitHub CLI and login

```bash
V=$(curl -sL -o /dev/null -w '%{url_effective}' https://github.com/cli/cli/releases/latest | sed 's#.*/v##')
curl -sL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${V}/gh_${V}_linux_arm64.tar.gz"
tar xzf /tmp/gh.tgz -C /tmp && mkdir -p ~/.local/bin && cp /tmp/gh_${V}_linux_arm64/bin/gh ~/.local/bin/
gh auth login            # GitHub.com → HTTPS → browser; enter the code at github.com/login/device
```

The token is stored in `~/.config/gh/`, so this is once per account. Make sure
`~/.local/bin` is on `PATH` (in `~/.bashrc`).

### Python environment

```bash
module load conda/2026.06
cd <repo> && python -m venv .venv
.venv/bin/pip install -r requirements.txt huggingface_hub hf_transfer
```

### Container image (vLLM)

```bash
export APPTAINER_CACHEDIR=$SCRATCH/apptainer-cache   # default is in home — too small
mkdir -p $SCRATCH/{images,hf,logs} $APPTAINER_CACHEDIR
cd $SCRATCH/images
APPTAINER_TMPDIR=/tmp apptainer pull vllm-0.27.1.sif docker://vllm/vllm-openai:v0.27.1
```

That is ~10.5 GB to download and becomes a 7.8 GB `.sif`. Use the image the
model card recommends for DGX Spark; `v0.27.1` has an arm64 build and is what
was tested.

### Model weights

```bash
export HF_HOME=$SCRATCH/hf/.cache
.venv/bin/hf download Inferact/Qwen3.8-27B-NVFP4 --local-dir $SCRATCH/hf/models/Qwen3.8-27B-NVFP4
```

That is 25 GB in a few minutes. No Hugging Face token is needed for ungated
models.

### Project repository clone

Use a **full** clone, not blobless. Triage tools `git grep` across the tree,
and in a blobless clone that downloads every file one by one. Zephyr's full
bare clone is ~1 GB and took about a minute.

```bash
git clone --bare https://github.com/<org>/<repo>.git candidates/<repo>.git
```

## 1.4 Two fixes vLLM needs on this machine

Both are built into `scripts/serve_triage_model.sh`. Without them it does not
work.

1. **Do not use `apptainer --nv`.**
   - It injects the host's OpenGL/Vulkan driver libraries too, and one of them
     crashes the container's glibc 2.35 at start-up with
     `Inconsistency detected by ld.so: dl-tls.c: 618: _dl_allocate_tls_init:
     Assertion 'listp != NULL' failed!`
   - Instead, bind only the four CUDA compute libraries (`libcuda.so.1`,
     `libnvidia-ml.so.1`, `libnvidia-nvvm.so.4`,
     `libnvidia-ptxjitcompiler.so.1`) into `/opt/hostnv`, and put that first
     on `LD_LIBRARY_PATH`. Run with `--cleanenv`.
2. **Pass `--safetensors-load-strategy eager`.**
   - vLLM's default memory-maps the weight files. On GB10, a GPU copy from
     memory-mapped data stalls for minutes per tensor, even from local NVMe:
     a 1.4 GB model never finished loading in 30 minutes.
   - Eager loading reads each shard into RAM first: the same model loads in
     under a second.

Also use `--gpu-memory-utilization 0.80`, not the 0.90 model cards suggest.
vLLM sees ~120 GiB but the job is capped at 112 GiB, and the API server and
client need room.

## 1.5 Serving a model

`triage/models.json` holds one profile per model: weights directory, vLLM
flags, environment variables and sampling settings. The serve script reads it.

```bash
cd <repo>                                            # sbatch from the repo root
sbatch scripts/serve_triage_model.sh qwen38-nvfp4    # → triage-server-<jobid>.log
squeue -u $USER                                      # note the job id and node
srun --jobid=<id> --overlap curl -s 127.0.0.1:8000/v1/models   # ready when this answers
```

Ready takes ~5–10 minutes for Qwen3.8 NVFP4 once compiled kernels are cached
(first start ~10 min), and ~16 minutes for Nemotron-3-Super.

The server job is independent of your desktop: closing the desktop does not
stop it, only its 4-hour limit or `scancel` does. When it ends, finished PRs
are already on disk; rerun any unfinished ones with `--pr`.

## 1.6 Running triage

```bash
srun --jobid=<id> --overlap -c 4 .venv/bin/python scripts/triage_prs.py run --profile qwen38-nvfp4 --gold
srun --jobid=<id> --overlap -c 4 .venv/bin/python scripts/triage_prs.py run --profile qwen38-nvfp4 --pr 43405 99824
.venv/bin/python scripts/triage_prs.py eval                    # rescore the latest run
```

Each run writes `triage/runs/<date>_<time>_<profile>/`:

- `manifest.json` — model, sampling, git commit, prompt hash
- `pr_triage.md` — a snapshot of the instructions used
- `<pr>.json` — the verdict
- `<pr>.trace.json` — the full conversation: the model's reasoning, every tool
  call and result, tokens, timings
- `scorecard.md` — the comparison with `triage/gold.json`

### Parallelism

- `--parallel N` (script) is how many PRs are in flight.
- `--max-num-seqs 4` (server) is how many requests the GPU batches together.

Measured on Qwen3.8 NVFP4:

| Requests at once | Total tokens/s | Per request |
|---|---|---|
| 1 | ~19–28 | ~19–28 |
| 2 | 26 | 13 |
| 4 | **43** | 11 |

Each PR is slower when sharing the GPU, but total throughput is ~2× higher.
CPU cores do not matter: the GPU's memory bandwidth is the limit.

## 1.7 Choosing a model

Generation speed on GB10 is set by **how many bytes of weights are read per
generated token**. GB10 memory bandwidth is ~273 GB/s; prompt reading is fast
(~1,700 tokens/s).

| Model | Weights | Fits | Decode | Notes |
|---|---|---|---|---|
| **Qwen3.8-27B NVFP4** (Inferact) | 25 GB | yes | ~28 tok/s | **Used.** 14/14 on the gold set; mean 8.6 min per PR |
| Qwen3.8-27B FP8 | 29 GB | yes | ~12 tok/s | Same judgement, 2.3× slower |
| Nemotron-3-Super-120B-A12B NVFP4 | 80 GB | batch only | ~26 tok/s | Misread the task on its one test PR; 16 min start-up |
| Qwen3.6-35B-A3B | 24–38 GB | yes | ~100 tok/s (reported) | Not tested |
| Qwen3.8-Flash-Next, DeepSeek-V4-Flash, GLM-5.3-Flash | 133–204 GB | **no** | — | Need 2+ Sparks |

**Adding a model:**

1. Download it to `$SCRATCH/hf/models/`.
2. Copy a profile in `triage/models.json`, taking the tool-call parser,
   reasoning parser and sampling from the model card or the vLLM recipe.
3. Check that the parser names exist:
   `vllm serve --help=all | grep -A6 tool-call-parser`.
4. Smoke-test it: start the server, then make one chat call and one tool call.
5. Run the gold set.

Profile flags that matter:

- `--enable-prefix-caching` — reuses the processed prompt across tool rounds,
  measured 15 s → 1.6 s for a 23K-token prompt. On hybrid models vLLM marks it
  experimental.
- `--language-model-only` — skips the vision tower of multimodal models.
- `--speculative-config` MTP — use it when the model ships an MTP head.
- `--reasoning-parser` — separates the thinking from the answer.
- `--enable-auto-tool-choice --tool-call-parser` — without the right parser,
  tool calls come back as plain text.

---

# Part 2 — The triage method, and adapting it to another project

## 2.1 What stays the same

`scripts/triage_prs.py` implements a method that is not specific to Zephyr:

1. **The whole enriched PR record goes in the prompt**, not behind tools. Our
   largest record is ~80K tokens; the context window is 262K. Each tool round
   costs a full pass of the model's thinking, so a fact we can include costs
   far less than a fact the model must look up.
2. **Read-only tools stay available** (`read_file`, `list_dir`, `grep` at the
   base or merge commit), for what the record cannot hold. They have a
   **budget** (8 rounds). When it is used up, the model is told to answer, and
   the last request is sent without tools. Every PR ends with a verdict.
3. **Verdicts are checked by code.** Required fields, allowed values, and test
   names that really occur in the suite. A failure goes back to the model with
   the reason, up to 2 times.
4. **Everything is saved per run**: verdicts, traces and a prompt snapshot.
5. **A gold set** of known accepts and rejects scores each run. Change **one
   thing at a time** and rerun the same PRs, so you know what helped.

## 2.2 What changes per project

| Piece | Zephyr version | For another project |
|---|---|---|
| Hard filters + enrichment (`filter_candidates.py`) | Zephyr PRs; test suites found by `testcase.yaml`; board files of our platforms | Your repo; how *your* tests are found and declared; your runnable targets' facts |
| Record rendering (`render_record`) | Zephyr sections | Your record's sections |
| Verdict checks (`check_verdict`) | `ZTEST(...)` names; our 4 Zephyr boards | Your test-name syntax; your runnable targets |
| Instructions (`docs/pr_triage.md`) | Zephyr | Written for your project |
| Gold set (`triage/gold.json`) | 8 hand-built instances + 6 rejects | Your validated instances + known rejects |

Unchanged: `serve_triage_model.sh`, `models.json`, and the run/eval/trace
machinery in `triage_prs.py`.

## 2.3 Lessons that cost us time

- **Measure where the time goes before tuning.** It was always generated
  tokens (98% hidden reasoning), not tools or prompt reading. Read the saved
  reasoning.
- **Give the model complete facts or none.** A partial board devicetree (with
  its `#include`d files missing) sent the model hunting through tools, and
  invited false rejects. We removed it.
- **Tool friction looks like model stubbornness.** A `grep` restricted to small
  directories (a blobless-clone workaround) caused an 8-round hunt for one
  Kconfig symbol. With a full clone it is one call.
- **State the requirement exactly.** Ours is "fails before the change, passes
  after it, on a target we can run". Scope what the model checks:
  - assume the change works where upstream CI ran it;
  - spend attention on what differs in *our* environment.
- **Know how your project picks targets before writing platform rules.** For
  Zephyr, the per-scenario keys (`platform_allow`, `arch_exclude`, `filter`,
  `integration_platforms`, …) and the board history mattered more than
  expected. Verify the rules against the project's own test runner, and
  against what your Docker images can actually build.
- **Gold data can be wrong too.** Two hand-built instances listed `pass_to_pass`
  tests that do not exist, and `metadata.json` omits the `extra_configs` the
  Dockerfiles use.

---

# Part 3 — Gotchas

| Symptom | Cause / fix |
|---|---|
| vLLM dies instantly with `dl-tls.c: 618` | `--nv` used — bind the compute libraries instead (1.4) |
| Server stuck at "Loading safetensors … 0%" for minutes | Memory-mapped loading — `--safetensors-load-strategy eager` (1.4) |
| Model too big or OOM on a desktop session | `spark-interactive` caps jobs at 60 GB — use `spark-batch` |
| A 10-minute request silently runs three times | LiteLLM's default 600 s timeout retries — set `timeout=3600, num_retries=0` |
| `sbatch` job unexpectedly slow, or `srun` step starved | 1 CPU by default — `--cpus-per-task=20` / `srun -c 4` |
| GitHub API "rate limit exceeded" | Shared IP — `gh auth login` |
| Apptainer fills home | Set `APPTAINER_CACHEDIR` on scratch before `apptainer pull` |
| `pkill -f <pattern>` or `pgrep -f` kills or matches your own shell | The pattern is in your own command line — kill by PID instead |
| `git commit`: "Author identity unknown" | No git identity on Scholar — `git config user.name/user.email` |
| `git push` asks for credentials | Use gh: `git -c credential.helper='!gh auth git-credential' push …` |
| Board file not found for recent Zephyr | `native_sim`'s metadata moved to `boards/native/native_sim/twister.yaml` (2026-09-26) |

---

## Files

| File | Purpose |
|---|---|
| `scripts/serve_triage_model.sh` | sbatch: vLLM in Apptainer, one profile, localhost |
| `triage/models.json` | Model profiles |
| `scripts/triage_prs.py` | `run` / `eval` |
| `triage/gold.json` | Gold verdicts |
| `docs/pr_triage.md` | Instructions (Zephyr; platform section under revision) |
| `scripts/filter_candidates.py` | Fetch, hard-filter and enrich PRs |
