# Apache Mynewt and Contiki-NG evaluation results

Four open-weight models ran on every Apache Mynewt task (61) and every Contiki-NG
task (37) on Purdue Scholar. The task lists ([Mynewt](../eval-sets/mynewt.txt),
[Contiki-NG](../eval-sets/contiki-ng.txt)) record the exact sets. The file layout
and CSV columns match the [Zephyr, RIOT, and NuttX results on main](https://github.com/DeveshM7/EmbedEval/tree/3087b25d101ab8df36f63e43e8c3ac7fbaae9ba9/results).

## Apache Mynewt (61 tasks)

Runs from 2026-11-19 to 2026-11-23 UTC.

| Model (profile in `triage/models.json`) | Passed | Failed | Hit the time limit |
|---|---:|---:|---:|
| MiniMax M2.5 ([`minimax-m25-nvfp4-thinkfix`](minimax-m25-nvfp4-thinkfix/mynewt/)) | 40/61 (65.6%) | 21 | 1 |
| MiniMax M2 ([`minimax-m2`](minimax-m2/mynewt/)) | 29/61 (47.5%) | 32 | 2 |
| GLM 4.6 ([`glm-46-nvfp4`](glm-46-nvfp4/mynewt/)) | 24/61 (39.3%) | 37 | 7 |
| gpt-oss-120b ([`gpt-oss-120b`](gpt-oss-120b/mynewt/)) | 28/61 (45.9%) | 33 | 0 |

The failure totals include the time limits in the last column. All 244 runs
have a final grade, with zero `not_run` grades.

## Contiki-NG (37 tasks)

Runs from 2026-11-26 to 2026-11-27 UTC.

| Model (profile in `triage/models.json`) | Passed | Failed | Hit the time limit |
|---|---:|---:|---:|
| MiniMax M2.5 ([`minimax-m25-nvfp4-thinkfix`](minimax-m25-nvfp4-thinkfix/contiki-ng/)) | 13/37 (35.1%) | 24 | 10 |
| MiniMax M2 ([`minimax-m2`](minimax-m2/contiki-ng/)) | 12/37 (32.4%) | 25 | 4 |
| GLM 4.6 ([`glm-46-nvfp4`](glm-46-nvfp4/contiki-ng/)) | 12/37 (32.4%) | 25 | 9 |
| gpt-oss-120b ([`gpt-oss-120b`](gpt-oss-120b/contiki-ng/)) | 13/37 (35.1%) | 24 | 0 |

The failure totals include the time limits in the last column. All 148 runs
have a final grade, with zero `not_run` grades. Thirty tasks are graded by
native unit tests. Seven are graded by Cooja network simulations: 385, 401,
1205, 1417, 1516, 3171, and 3212. Every pass above is on a native task. No
model passed a Cooja task.

## Setup

- mini-SWE-agent 2.4.6, bash only, with 250 steps and 120 minutes per task
  and 900 seconds per command. Agents ran in network-isolated containers with
  the grading tests hidden. Submitted patches were graded in fresh containers.
- vLLM 0.27.1 served the models on Scholar's GB10 nodes. Agents, builds, and
  Contiki-NG's simulations ran on x86_64 CPU nodes. Model weights, revisions,
  and sampling are in [triage/models.json](../triage/models.json).
- Both MiniMax profiles used the closing `</think>` repair. The 31 earlier
  M2.5 Mynewt runs from before that repair are excluded.

## Files

- [summary.csv](summary.csv) contains one row per model, project, and task,
  with the grade, exit status, steps, elapsed minutes, limit flags, grading
  error, and output repair counts.
- `<profile>/<project>/<PR>/` contains `grade.json`, `run.json`, and
  `patch.diff`. The import checked these files against every job's API key
  (Mynewt's 33 and Contiki-NG's 8). The records and patches match the
  exported bytes, with nothing redacted.
- Trajectories and job logs remain on Scholar under
  `$SCRATCH/embedeval/eval-full-1118`, in each model's results and logs
  directories.
