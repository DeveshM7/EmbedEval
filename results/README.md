# Apache Mynewt evaluation results (2026-11-19 to 2026-11-23 UTC)

Four open-weight models ran on all 61 Apache Mynewt tasks on Purdue Scholar.
The [task list](../eval-sets/mynewt.txt) records the exact set. The file layout
and CSV columns match the [Zephyr, RIOT, and NuttX results on main](https://github.com/DeveshM7/EmbedEval/tree/3087b25d101ab8df36f63e43e8c3ac7fbaae9ba9/results).

| Model (profile in `triage/models.json`) | Passed | Failed | Hit the time limit |
|---|---:|---:|---:|
| MiniMax M2.5 ([`minimax-m25-nvfp4-thinkfix`](minimax-m25-nvfp4-thinkfix/mynewt/)) | 40/61 (65.6%) | 21 | 1 |
| MiniMax M2 ([`minimax-m2`](minimax-m2/mynewt/)) | 29/61 (47.5%) | 32 | 2 |
| GLM 4.6 ([`glm-46-nvfp4`](glm-46-nvfp4/mynewt/)) | 24/61 (39.3%) | 37 | 7 |
| gpt-oss-120b ([`gpt-oss-120b`](gpt-oss-120b/mynewt/)) | 28/61 (45.9%) | 33 | 0 |

The failure totals include the time limits in the last column. All 244 runs
have a final grade, with zero `not_run` grades.

## Setup

- mini-SWE-agent 2.4.6, bash only, with 250 steps and 120 minutes per task
  and 900 seconds per command. Agents ran in network-isolated containers with
  the grading tests hidden. Submitted patches were graded in fresh containers.
- vLLM 0.27.1 served the models on Scholar's GB10 nodes. Agents and Mynewt's
  native builds ran on x86_64 CPU nodes. Model weights, revisions, and sampling
  are in [triage/models.json](../triage/models.json).
- Both MiniMax profiles used the closing `</think>` repair. The 31 earlier
  M2.5 runs from before that repair are excluded.

## Files

- [summary.csv](summary.csv) contains one row per model and task, with the grade,
  exit status, steps, elapsed minutes, limit flags, grading error, and output
  repair counts.
- `<profile>/mynewt/<PR>/` contains `grade.json`, `run.json`, and `patch.diff`.
  The import checked these files for Scholar job keys. The records and patches
  matched the exported bytes without any redactions.
- Trajectories and job logs remain on Scholar under
  `$SCRATCH/embedeval/eval-full-1118` in each model's results and logs directories.
