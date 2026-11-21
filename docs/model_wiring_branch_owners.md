# Running the four evaluation models: what to change on your branch

For the NuttX, Mynewt and Contiki-NG branch owners. `main` (038cee7, pushed 2026-11-16) can now run the four
open-weight models we will evaluate. This lists every change we had to make, so that you can do the same on your
branch and machines. The code is on `main`: copy it, or merge `main` and keep your project's parts.

**Updated 2026-11-18, after the full Zephyr runs of all four models:** one more model fix
(MiniMax M2, section 3b), the memory and node settings that worked (section 4), and how to run reliably on the
GB10 nodes (section 5b). The M2 fix and the multi-node job files are not on `main` yet; section 3b gives the fix
in full, so you can add it now.

**Updated 2026-11-21, after the full RIOT runs:** MiniMax M2.5 now also gets the closing-tag repair: **run M2.5
with the profile `minimax-m25-nvfp4-thinkfix` only** (section 3b). Our M2.5 results without the repair are set
aside. Also: agents killed by memory on shared nodes, and more nodes to exclude (section 5b).

**The models** (chosen after pilots on 5 Zephyr/RIOT tasks each):

| Model | Profile (`triage/models.json`) | Weights (Hugging Face, revision) | Precision | Agents per server | Pilot (5) | SWE-bench Verified, bash-only |
|---|---|---|---|---|---|---|
| MiniMax M2.5 | `minimax-m25-nvfp4-thinkfix` | `nvidia/MiniMax-M2.5-NVFP4` @ `b6220d658389629b9d507d4b2bb314f41fea7898` | NVFP4 (~126 GB) | 2 | 4/5 | 75.8 |
| MiniMax M2 | `minimax-m2` | `MiniMaxAI/MiniMax-M2` @ `04312f0cfbdeefb912575595b7c045512a29293c` | FP8 (~215 GB) | 2 | 2/5 (before section 3b's fix) | 61.0 |
| GLM 4.6 | `glm-46-nvfp4` | `RedHatAI/GLM-4.6-NVFP4` @ `67d1628eac13f95ac8648600d68356390abe1c95` | NVFP4 (~192 GB) | 3 | 4/5 | 55.4 |
| gpt-oss-120b | `gpt-oss-120b` | `openai/gpt-oss-120b` @ `b5c939de8f754692c1647ca79fbf85e8c1e70f8a` | MXFP4 (~60 GB) | 3 | 4/5 | 26.0 |

Nodes per server and memory share depend on the kind of node: see section 4.

Our aim is to compare with the **published** bash-only SWE-bench Verified scores (mini-SWE-agent, the agent we
use), so the agent setup must match theirs as closely as possible. That is why sections 2 and 3 matter.

---

## 1. Model profiles: one entry per model in `triage/models.json`

Copy the four entries `minimax-m25-nvfp4-thinkfix`, `minimax-m2`, `glm-46-nvfp4` and `gpt-oss-120b` from `main` as
they are (`minimax-m25-nvfp4`, M2.5 without the repair, stays only for older runs: do not evaluate with it).
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
| MiniMax M2, MiniMax M2.5 (`minimax-m25-nvfp4-thinkfix`) | `"repair_think_close": true` | see section 3b (not on `main` yet) |
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

Without these, gpt-oss lost runs to format errors, not to the task. GLM 4.6 needs no repairs; for MiniMax M2 and
M2.5, see section 3b.

## 3b. Closing-tag repair, for MiniMax M2 and M2.5 (added 2026-11-17, updated 2026-11-21)

**The problem.** With tool calls, vLLM 0.27.1's MiniMax tool-call parser (`minimax_m2`, with the reasoning parser
`minimax_m2_append_think`) returns the reply's content as `<think>reasoning...` **without the closing `</think>`**
(a plain reply, without a tool call, keeps it). The agent keeps that content in the conversation, so on every later
turn M2 sees its earlier reasoning as an unclosed block; MiniMax says M2's reasoning must stay intact in the
history. M2 then looped (one `sed` repeated 178 times) and ran out of steps on every problem; with the fix it
submits and passes normally (on a 4-problem test: 0 submitted before, 4 submitted and 2 passed after).
The published runs did not go through this parser (M2's used mini-swe-agent 1.17 text actions), so this is our
set-up's problem, not the model's.

**The fix** (`harness/run.py`, applied only to profiles with `"repair_think_close": true`, i.e. `minimax-m2`): before
mini-swe-agent stores the reply, a reply with tool calls whose content starts with `<think>` and has no `</think>`
gets `</think>` at its end. Nothing else is touched; the count goes to `run.json` as `think_repairs`.

```python
def add_think_repair(model, full_model: str) -> None:
    profile = LOCAL_MODELS.get(full_model)
    if not profile or not json.loads((paths.REPO_ROOT / "triage" / "models.json").read_text())[profile].get("repair_think_close"):
        return
    parse = model._parse_actions
    model.think_repairs = 0

    def _parse_actions(response):
        message = response.choices[0].message
        content = message.content if isinstance(message.content, str) else ""
        if message.tool_calls and content.lstrip().startswith("<think>") and "</think>" not in content:
            message.content = content.rstrip() + "\n</think>"
            model.think_repairs += 1
        return parse(response)

    model._parse_actions = _parse_actions
```

Call it right after `add_tool_arg_repair(model, full_model)` in `run_one`, add `"repair_think_close": true` to the
`minimax-m2` profile, and record `agent.model.think_repairs` in `run.json` as `tool_arg_repairs` is. Check: in a run's
`trajectory.json`, every assistant message of M2 should end with `</think>`.

**MiniMax M2.5 needs it too (added 2026-11-21).** M2.5 goes through the same parser and has the same problem:
on RIOT, all 4,782 of its replies with tool calls had an unclosed `<think>`. Unlike M2 it still worked well without
the repair, so we first ran it as it was. But it took many more steps than the other models (median about 116 per
problem on RIOT, against 73-77), and the published M2.5 run never had the problem: it used a hosted API
(`minimax/minimax-m2.5`), which returns the reasoning in separate `reasoning` / `reasoning_details` fields and
passes it back intact, with no `<think>` tags in the content at all. So we ran Zephyr and RIOT again with the
repair: M2.5 took fewer steps and did at least as well. **Only the runs with the repair count**, for every project.

The profile is `minimax-m25-nvfp4-thinkfix`: a copy of `minimax-m25-nvfp4` (same weights, revision, server settings
and sampling) with `"repair_think_close": true` added. Its results go to their own folder
(`<eval dir>/minimax-m25-nvfp4-thinkfix/<project>/`). If you already ran M2.5 without the repair, set those
results aside and run it again with this profile:

  ```json
  "minimax-m25-nvfp4-thinkfix": {
      "weights": "MiniMax-M2.5-NVFP4",
      "revision": "b6220d658389629b9d507d4b2bb314f41fea7898",
      "nodes": 2,
      "vllm_args": [... the same as minimax-m25-nvfp4 ...],
      "env": {},
      "repair_think_close": true,
      "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 40},
      "max_tokens": 32768,
      "max_model_len": 196608
  }
  ```

Same layouts and agents per server as `minimax-m25-nvfp4` (section 4). Check: `think_repairs` in `run.json` should
equal the run's steps.

## 4. Serving the models (vLLM, `scripts/serve_triage_model.sh`)

- **vLLM 0.27.1** in Apptainer: `apptainer pull vllm-0.27.1.sif docker://vllm/vllm-openai:v0.27.1` into scratch
  (`$EMBEDEVAL_VLLM_IMAGE`). Two settings are required on GB10, not tuning: no `--nv` (bind only the four CUDA
  libraries, as the script does: `--nv` crashes the container's glibc), and `--safetensors-load-strategy eager`
  (memory-mapped loading stalls for minutes per tensor).
- **One model over several nodes** (76da297): every model but gpt-oss needs several nodes (layouts below). Each node runs the script
  with `VLLM_NNODES` (nodes), `VLLM_NODE_RANK` (0, 1, ...) and `VLLM_MASTER_ADDR` (rank 0's address). This is
  pipeline parallel, one stage per node: rank 0 serves the API, the others run `--headless`. NCCL runs **over TCP**
  (`NCCL_IB_DISABLE=1`: its RDMA path fails in the container, "unhandled system error"), on interface
  `VLLM_NCCL_IFNAME` (default `enp1s0f0np0`, Scholar's 10 Gb/s link between Spark nodes).
- **Layouts that worked in the full Zephyr runs.** Scholar has two kinds of GB10 node: `spark-batch` (whole node,
  ~124 GB) and `spark-interactive` (a job gets at most **60 GB** of a node, so a model needs about twice the nodes).
  The memory share is vLLM's `--gpu-memory-utilization` (`GPU_MEMORY`); the rest of the node is for the agents'
  builds and QEMU.

  | Model | spark-batch | spark-interactive (60 GB per node) | Room at full context* |
  |---|---|---|---|
  | gpt-oss-120b | 1 node at **0.80** (at 0.70 one server could not start and the rest had too little KV for 3 agents) | not used | 3.4-4.1 |
  | MiniMax M2.5 | 2 nodes at 0.80 (only ~3-6 GB left for builds: avoid) | **6 nodes at 0.42** | ~1.8 |
  | MiniMax M2 | **4 nodes at 0.65** | **8 nodes at 0.38** | 2.4-2.8 |
  | GLM 4.6 | **4 nodes at 0.65** | **10 nodes at 0.38** (8 gave only 1.4) | 1.7-3.5 (see the next point) |

  *Full-length requests the server can hold at once ("Maximum concurrency" in vLLM's log); it should be at least the
  number of agents (2.2 or more for 3 agents). If a server comes up lower, run fewer agents on it.
- **Uneven layer split (GLM over 10 nodes):** vLLM gives the leftover layers to some stages ("Hidden layers were
  unevenly partitioned: [9,9,9,9,9,9,9,10,10,9]"), and that node has much less KV room, so 3 of 5 GLM servers came up
  below 2.2. `VLLM_PP_LAYER_PARTITION` can place the extra layers elsewhere (not tried yet).
- **Start-up time:** the scratch disk delivers ~0.75 GB/s in total, shared by all servers loading at once: 9 M2
  servers (~2 TB) took 45-50 min. Wait **90 min** before calling a server failed (a 40-min limit killed 6 of 9
  servers that were still loading), and stagger the servers' starts (5 min apart worked), so the first ones start
  working early.
- **Agents per server** = the server's `--max-num-seqs`: MiniMax M2.5 and M2 2, GLM 4.6 and gpt-oss 3. More agents
  make every agent slower; the 2-hour cap then cuts runs off. Spread a server's agents over its nodes (agent *k* on
  rank *k*), so each node does only its own agent's builds.
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

## 5b. Running reliably on the GB10 nodes (learned in the full runs)

- **Nodes freeze.** Six GB10 nodes stopped responding during our runs (five of them `spark-batch`), and others froze
  or rebooted without us. A frozen node takes its whole server with it, and by default Slurm then cancels the
  whole job ("CANCELLED DUE TO NODE FAILURE"). So:
  - submit with **`--no-kill`**, so a failed node does not end the job;
  - launch **one `srun` step per server group**, so a failed node aborts only its own group;
  - run a **watchdog** on each agent node: stop that node's agents if the server stops answering for 3 minutes,
    **or** if `vllm:generation_tokens_total` (from the server's `/metrics`) has not moved for 10 minutes while
    `vllm:num_requests_running` > 0. Without it, agents run problem after problem against a dead server (each
    marked "not run" after two attempts), or wait for hours on a hung one (`/v1/models` can keep answering while
    generation has stopped).
- **Some nodes come up short of memory** (k015, l006, k060 on Scholar), so their server cannot start or has little
  KV room; exclude them (`-x`). Nodes that froze in our runs, also worth excluding: k054, k055, k063, k069, k070,
  k071, k076 (k062 is not available to us).
- **An agent's command can run a shared node out of memory.** On RIOT's x86 `cpu` nodes (8 agents per node), some
  MiniMax builds, after the model had edited build files or headers, used up the node's memory. The kernel then
  killed whole agent processes, the culprit's or a neighbour's, and their runs ended with no record (only
  `trajectory.json`; Slurm reports the step as `OUT_OF_MEMORY`). Our harness limits each command's time (900 s),
  not its memory, and we kept it that way (a limit could change the earlier models' results). Our rule: a run whose
  own commands caused it (for example, the only problem left on the node) counts as a fail; a run that may have been
  a neighbour's victim is retried. If your agents share nodes, check `sacct` for `OUT_OF_MEMORY` after each job.
- **Retry, do not count, what the cluster broke:** runs cut off by a dead server or a job's end are infrastructure
  errors; move their folders aside so they restart fresh rather than using up their second attempt.
- **Resubmission:** our jobs resubmit themselves while problems are left; set `EVAL_MAX_JOBS=1` when you would
  rather decide each follow-up yourself, and size a follow-up to the problems left.
- Our job files for all of this (`cluster/templates/eval_zephyr_multi.sbatch`, `eval_zephyr_het.sbatch`,
  `scripts/eval_zephyr_group.sh`, and for RIOT `cluster/templates/eval_riot_multi.sbatch` with
  `scripts/eval_riot_remote.sh`) are not on `main` yet.

## 6. What to expect, to check your set-up

From our pilots (Zephyr and RIOT, 5 tasks per model, on GB10; MiniMax M2 before its fix):

| Model | Generation speed | Time per task | Behaviour |
|---|---|---|---|
| MiniMax M2.5 (2 nodes, 2 agents) | ~13 tok/s per agent | 14-83 min | steady |
| MiniMax M2 (3 nodes, 2 agents) | ~8 tok/s per agent | 15-71 min | short replies, many small steps; Zephyr tasks hit the 250-step limit |
| GLM 4.6 (3 nodes, 3 agents) | slowest | 35-97 min | steady, but closest to the 2-h cap |
| gpt-oss-120b (1 node, 3 agents) | ~20-30 tok/s per agent | 15-66 min | needs the repairs in section 3 (without them, runs ended on format errors within 3-9 min) |

In the full runs, GLM 4.6 and MiniMax M2 took about an hour per problem, M2.5 under an hour, gpt-oss about half an
hour; GLM hit the 2-hour cap most often.

Signs that something is set up wrong, not the model: many format errors (check the tool-call parser and the
templates in section 2), a context overflow after one large command (output truncation missing), or the same
command repeated tens of times (we saw this with Devstral, dropped for it, and with MiniMax M2 before section 3b's
fix: check that its reasoning is closed in the conversation).

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
