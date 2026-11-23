# Full evaluation results (2026-11-16 to 2026-11-23)

Four open-weight models on 122 problems: Zephyr (50, sampled), RIOT (43, sampled) and
NuttX (all 29). The problem lists are in `eval-sets/` (`zephyr.txt`, `riot.txt`, `nuttx.txt`).
Mynewt and Contiki-NG are run by their branch owners.

| Model (profile in `triage/models.json`) | Zephyr | RIOT | NuttX | All 122 | SWE-bench Verified, bash-only |
|---|---|---|---|---|---|
| MiniMax M2.5 (`minimax-m25-nvfp4-thinkfix`) | 36/50 (72.0%) | 24/43 (55.8%) | 19/29 (65.5%) | 79 (64.8%) | 75.8% |
| MiniMax M2 (`minimax-m2`) | 29/50 (58.0%) | 22/43 (51.2%) | 16/29 (55.2%) | 67 (54.9%) | 61.0% |
| GLM 4.6 (`glm-46-nvfp4`) | 25/50 (50.0%) | 20/43 (46.5%) | 14/29 (48.3%) | 59 (48.4%) | 55.4% |
| gpt-oss-120b (`gpt-oss-120b`) | 20/50 (40.0%) | 18/43 (41.9%) | 12/29 (41.4%) | 50 (41.0%) | 26.0% |

## Setup

- Agent: mini-SWE-agent 2.4.6, bash only. Limits per problem: 250 steps, 2 hours,
  900 s per command. No network in the agent's container; the tests that grade a run are
  hidden from the agent.
- Serving: vLLM 0.27.1 on the cluster's GB10 nodes, pipeline parallel across nodes for the
  larger models; weights and revisions as pinned in `triage/models.json`, sampling as there.
- MiniMax M2 and M2.5 run with the closing `</think>` repair (`repair_think_close`, see
  `harness/run.py`). Only the repaired M2.5 runs count; earlier M2.5 runs without it are
  not included here.
- A run stopped by the infrastructure (a job's end, a failed node, a possible out-of-memory
  victim) was rerun from scratch; a model's own mistakes, including runaway commands,
  count as failures.

## Files

- `summary.csv`: one row per run: model, project, instance, grade (pass / fail), exit
  status, steps, minutes, whether it hit the time or step limit, the grading error, and
  the number of output repairs.
- `<profile>/<project>/<problem>/`: `grade.json` (the hidden-test grading), `run.json`
  (the run's record), `patch.diff` (the agent's patch).
- The trajectories (every message of every run) and the job logs are in the GitHub release
  `results-2026-11-23` (`embedeval-results-2026-11-23.tar.gz`), too large for the repository.

## Changes to the raw outputs

- API keys (one per job, valid only while that job ran) are replaced by `REDACTED`.
- Three RIOT trajectories had one command output of about 25 GB each: the agent ran a
  test program that waits for keyboard input, and it printed its prompt
  (`Help: Press s to start test, r to print it is ready`) until the 900 s command limit.
  The model only saw the usual shortened output. In those files each such string is cut
  to its first 20,000 and last 5,000 bytes, with a marker in between
  (`trajectory_trimmed` in `summary.csv`): MiniMax M2 on 17342, MiniMax M2.5 on 20720 and
  GLM 4.6 on 20676. All three are failures in either case.
