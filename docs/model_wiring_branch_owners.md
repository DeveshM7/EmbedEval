# Running the four evaluation models: what to change on your branch

For the NuttX, Mynewt and Contiki-NG branch owners. `main` (038cee7, pushed 2026-11-16) can now run the four
open-weight models we will evaluate. This lists every change we had to make, so that you can do the same on your
branch and machines. The code is on `main`: copy it, or merge `main` and keep your project's parts.

**The models** (chosen after pilots on 5 Zephyr/RIOT tasks each):

| Model | Profile (`triage/models.json`) | Weights (Hugging Face, revision) | Precision | GB10 nodes | Agents per server | Pilot | SWE-bench Verified, bash-only |
|---|---|---|---|---|---|---|---|
| MiniMax M2.5 | `minimax-m25-nvfp4` | `nvidia/MiniMax-M2.5-NVFP4` @ `b6220d658389629b9d507d4b2bb314f41fea7898` | NVFP4 | 2 | 2 | 4/5 | 75.8 |
| MiniMax M2 | `minimax-m2` | `MiniMaxAI/MiniMax-M2` @ `04312f0cfbdeefb912575595b7c045512a29293c` | FP8 | 3 | 2 | 2/5 | 61.0 |
| GLM 4.6 | `glm-46-nvfp4` | `RedHatAI/GLM-4.6-NVFP4` @ `67d1628eac13f95ac8648600d68356390abe1c95` | NVFP4 | 3 | 3 | 4/5 | 55.4 |
| gpt-oss-120b | `gpt-oss-120b` | `openai/gpt-oss-120b` @ `b5c939de8f754692c1647ca79fbf85e8c1e70f8a` | MXFP4 | 1 | 3 | 4/5 | 26.0 |

Our aim is to compare with the **published** bash-only SWE-bench Verified scores (mini-SWE-agent, the agent we
use), so the agent setup must match theirs as closely as possible. That is why sections 2 and 3 matter.

---

## 1. Model profiles: one entry per model in `triage/models.json`

Copy the four entries `minimax-m25-nvfp4`, `minimax-m2`, `glm-46-nvfp4` and `gpt-oss-120b` from `main` as they are.
Each holds:

- `weights` (folder under `$EMBEDEVAL_MODELS`, default `/scratch/scholar/$USER/hf/models`) and `revision`;
- `nodes` (how many GB10 nodes the server needs; 1 if absent);
- `vllm_args` and `env` (`{models}` expands to the weights root, `{repo}` to the repository);
- `sampling` (temperature, top_p, top_k), `max_tokens` (32768) and `max_model_len` (the context).

Settings that differ from vLLM's defaults, and why (all found the hard way, on GB10):

| Model | Setting | Why |
|---|---|---|
| MiniMax M2 | env `VLLM_USE_DEEP_GEMM=0`, `VLLM_MOE_USE_DEEP_GEMM=0` | DeepGEMM's FP8 kernels fail on GB10: `Assertion error (deepgemm .../layout.hpp:60): Unknown SF transformation` |
| MiniMax M2, GLM 4.6 | `--kernel-config '{"enable_flashinfer_autotune": false}'` | over 3 nodes, FlashInfer's MoE autotuning hung on node 0 (no error, no progress) |
| GLM 4.6 | `max_model_len` 131072 (of 203k) | leaves room for the KV cache over 3 nodes |
| gpt-oss-120b | env `TIKTOKEN_ENCODINGS_BASE={models}/tiktoken-gpt-oss` | its Harmony format downloads two vocabulary files at start, and the server has no network. Put `o200k_base.tiktoken` and `cl100k_base.tiktoken` (from `https://openaipublic.blob.core.windows.net/encodings/`) in that folder |
| gpt-oss-120b | `"repair_tool_args": true` | see section 3 |
| MiniMax M2.5/M2 | `--trust-remote-code`, `--reasoning-parser minimax_m2_append_think`, `--tool-call-parser minimax_m2` | from the model cards |
| GLM 4.6 | `--reasoning-parser glm45`, `--tool-call-parser glm45` | from the model card |
| all | `--enable-auto-tool-choice` with each model's tool-call parser | mini-SWE-agent 2.x uses tool calls |

**Sampling:** the published run's settings where they were recorded, else the model card's: MiniMax M2.5 and M2
temperature 1.0, top_p 0.95, top_k 40 (M2's published run used 0.0; we use 1.0, by decision); GLM 4.6
temperature 1.0; gpt-oss temperature 1.0, top_p 1.0. Use the same on every project, so the projects compare.

**Weights:** download each at its revision, into scratch (never home: 25 GB quota). We used
`huggingface_hub.snapshot_download(repo_id=..., local_dir=..., revision=..., max_workers=8)`. The four take
about 625 GB together; download one, run it, delete it, if space is short (scratch quota is 1 TB).

## 2. Harness: every profile is a model

`harness/paths.py` and `harness/run.py`:

- **Every profile is a local model,** named `openai/<profile>` (`paths.LOCAL_PROFILES`, `run.LOCAL_MODELS`),
  instead of a hand-kept list. `--model gpt-oss-120b` or `--model openai/gpt-oss-120b` both work.
- **`run.model_config`** builds mini-SWE-agent's model settings from the profile: the server address
  (`EMBEDEVAL_LOCAL_API_BASE`, default `http://127.0.0.1:8000/v1`) and key (`EMBEDEVAL_LOCAL_API_KEY`); the sampling;
  `top_k`, `min_p` and `repetition_penalty` go in `extra_body` (vLLM reads them there, LiteLLM does not pass them
  otherwise); `max_tokens`; `timeout=3600` (LiteLLM's 600 s default retried long generations silently);
  `num_retries=0`; and `cost_tracking: "ignore_errors"` (a local model has no price, and LiteLLM's lookup fails).
- **The SWE-bench run's templates** (commit 36613d3), copied from mini-SWE-agent 2.4.6's
  `benchmarks/swebench.yaml` into `run.py` (`FORMAT_ERROR_TEMPLATE`, `OBSERVATION_TEMPLATE`) and set on the model:
  - a format error now shows the model the **actual error** (before, every error said "No tool call found", so a
    model with a malformed call could never correct it);
  - command output over 10,000 characters is **cut to its first and last 5,000** with a warning (before, output
    was never cut: one `ls -R` of Zephyr was 547k tokens and overflowed a 131k context).

  These two matter most for a fair comparison: the published runs had both. Results from before this change are
  not comparable.
- **Limits:** 250 steps, 2-hour cap per run, 900 s per command (the published runs used 60 s; our builds need
  more).

## 3. Tool-call repairs, for gpt-oss-120b only

gpt-oss writes some tool calls in shapes mini-SWE-agent rejects. A profile with `"repair_tool_args": true` gets
two narrow repairs (`run.add_tool_arg_repair`), counted in `run.json` as `tool_arg_repairs`:

1. **A stray `]`** (95b24b2): it closes `{"command": "..."}` as if the command were a list, `..."]}` (the shape of
   the shell tool it was trained on): invalid JSON, a format error, and runs ended on RepeatedFormatError. Arguments
   that are invalid JSON, end in `"]}`, and become a valid `{"command": <string>}` without the `]` are fixed;
   nothing else is touched.
2. **The submission written as text** (d6ab341): it sometimes writes the submit command in its final answer instead
   of a tool call. A reply with no tool call whose whole text is `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
   (bare, or as `{"command": ...}`) is taken as that call. No other text is.

Without these, gpt-oss lost runs to format errors, not to the task. The other three models need no repairs (0
format errors in their pilots).

## 4. Serving the models (vLLM, `scripts/serve_triage_model.sh`)

- **vLLM 0.27.1** in Apptainer: `apptainer pull vllm-0.27.1.sif docker://vllm/vllm-openai:v0.27.1` into scratch
  (`$EMBEDEVAL_VLLM_IMAGE`). Two settings are required on GB10, not tuning: no `--nv` (bind only the four CUDA
  libraries, as the script does: `--nv` crashes the container's glibc), and `--safetensors-load-strategy eager`
  (memory-mapped loading stalls for minutes per tensor).
- **One model over several nodes** (76da297): MiniMax M2.5 needs 2 nodes, M2 and GLM 3. Each node runs the script
  with `VLLM_NNODES` (nodes), `VLLM_NODE_RANK` (0, 1, ...) and `VLLM_MASTER_ADDR` (rank 0's address). This is
  pipeline parallel, one stage per node: rank 0 serves the API, the others run `--headless`. NCCL runs **over TCP**
  (`NCCL_IB_DISABLE=1`: its RDMA path fails in the container, "unhandled system error"), on interface
  `VLLM_NCCL_IFNAME` (default `enp1s0f0np0`, Scholar's 10 Gb/s link between Spark nodes).
- **Start-up time:** a 3-node server takes 25-40 min to load (MiniMax M2: ~14 min of weight loading alone), so wait
  up to 40 min before calling it failed (`EVAL_SERVER_WAIT_MIN` in the pilot scripts).
- **Agents per server** = the server's `--max-num-seqs`: MiniMax M2.5 and M2 2, GLM 4.6 and gpt-oss 3. More agents
  make every agent slower; the 2-hour cap then cuts runs off.
- **A server on another node** (agents on x86 `cpu` nodes, as RIOT's are): `HOST=0.0.0.0` and an API key
  (`VLLM_API_KEY`) are required; the agents get the address and key through `EMBEDEVAL_LOCAL_API_BASE` and
  `EMBEDEVAL_LOCAL_API_KEY`.
- **On GB10, a stopped vLLM server's GPU memory is not freed until the Slurm job ends**, so a failed server cannot be
  restarted inside the same job: start a new job.
- **One job for several models:** `cluster/templates/eval_pilot.sbatch` (with `scripts/eval_pilot_server.sh`,
  `scripts/eval_pilot_riot.sh`) runs several models side by side in one heterogeneous job (Scholar counts it as
  one of the two jobs you may run). `EVAL_PILOT_PLAN` lists `<profile> <nodes> <zephyr workers> <riot workers>` per
  line. Adapt the worker part to your project's runner.

## 5. Reliability fixes worth taking

- **Clean-up on NFS scratch** (2df4815): `/scratch/scholar` is NFS. A folder in which some process still holds a file
  open cannot be deleted (`[Errno 39] Directory not empty`, then `[Errno 16] Device or resource busy`), and an error
  in clean-up was hiding the real error. `apptainer_util.remove` now stops the processes holding that file or folder
  and retries; `remove_quietly` is used for clean-up after an error (it prints, never raises); image pulls keep
  their temporary files in the work root, not the node's small `/tmp`. If your runner deletes run folders or images
  on scratch (Docker or Apptainer), do the same.
- **Tracebacks:** `run.json` now records the traceback of any error (`traceback`), printed for infrastructure
  errors, and a failed environment clean-up is printed instead of ignored. A one-line `OSError` was not enough to
  find a cause.

## 6. What to expect, to check your set-up

From our pilots (Zephyr and RIOT, 5 tasks per model, on GB10):

| Model | Generation speed | Time per task | Behaviour |
|---|---|---|---|
| MiniMax M2.5 (2 nodes, 2 agents) | ~13 tok/s per agent | 14-83 min | steady |
| MiniMax M2 (3 nodes, 2 agents) | ~8 tok/s per agent | 15-71 min | short replies, many small steps; Zephyr tasks hit the 250-step limit |
| GLM 4.6 (3 nodes, 3 agents) | slowest | 35-97 min | steady, but closest to the 2-h cap |
| gpt-oss-120b (1 node, 3 agents) | ~20-30 tok/s per agent | 15-66 min | needs the repairs in section 3 (without them, runs ended on format errors within 3-9 min) |

Signs that something is set up wrong, not the model: many format errors (check the tool-call parser and the
templates in section 2), a context overflow after one large command (output truncation missing), or the same
command repeated tens of times (we saw this with Devstral, dropped for it).

## Commits on `main`, for reference

| Commit | What |
|---|---|
| 32d1d13 | model profiles; every profile a model; per-profile context |
| 76da297 | one model over several nodes (and a Qwen parser plugin, no longer used) |
| f8ab156 | several models in one pilot job |
| 36613d3 | the SWE-bench run's format-error and output-truncation templates |
| 95b24b2 | gpt-oss stray `]` repair |
| d6ab341 | gpt-oss submission-as-text repair |
| 2df4815 | NFS clean-up fix, tracebacks |
| 038cee7 | GLM 4.6 and MiniMax M2 profiles, weight revisions |

## Also needed: the evaluation rules already on `main`

These came before the model work; your branch needs them too for results that compare:

1. **Hide the PR's tests from the agent.** At the start of each run (in the run's overlay only): undo the test
   diff, delete `/opt/embedeval/test_patch.diff`, strip test names from the runner's settings, remove leftovers
   that reveal the tests (prebuilt output, indexes), and replace `run_tests` with one that runs the tests the agent
   names. Grading stays as is. See `harness/hidden_tests.py` and `harness/agent_run_tests/`.
2. **Prompt:** say the tests are hidden; tell the agent to write its own tests and to submit once its change and
   tests pass; add "time is limited". The text is in `harness/hidden_tests.py`.
3. **Use the verified statements:** use an instance's `problem_statement_fixed` when present (`statement_of` in
   `harness/run.py`).
4. **Limits:** 250 steps and a 2-hour cap per run (section 2).
5. **Grade only submitted runs,** and retry an infrastructure error once before counting the run "not run".
