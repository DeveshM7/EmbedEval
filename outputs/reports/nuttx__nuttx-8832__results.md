# nuttx__nuttx-8832 — wqueue: drain all pending work items per wakeup

**Task:** Fix `sched/wqueue/kwork_thread.c` so `wqueue_test` sees all 100 queued workers complete (not just the first one per wakeup cycle).  
**Date:** 2026-04-25

| Model               | Exit Status    | Steps | Patch Correct | Notes                                                          |
|---------------------|----------------|-------|---------------|----------------------------------------------------------------|
| openai/gpt-5.4      | Submitted      | 4     | Yes           | Changed single `if` to `while` loop draining all work items; `continue` for null workers. Clean minimal fix. |
| anthropic/claude-opus-4-6 | Submitted | 13   | Yes           | Same while loop + `nxsem_trywait` to consume extra semaphore counts for each additional item drained. More robust. |
| gemini/gemini-2.5-pro | LimitsExceeded | 42  | No            | Garbled patch: `dq_remfirst(if (work->worker)wqueue->q)`, misplaced braces. Never assembled a valid diff after 42 steps. |

## Verdict

- **GPT-5.4**: correct and efficient (4 steps).
- **Claude Opus 4.6**: correct and most robust (handles semaphore over-count edge case), but takes 3× more steps.
- **Gemini 2.5 Pro**: hit step limit with a syntactically broken patch — still confused after 42 steps.
