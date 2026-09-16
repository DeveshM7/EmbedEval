# nuttx__nuttx-11898 — pthread: fix pthread_join returning ESRCH instead of EINVAL

**Task:** Fix `pthread_findjoininfo.c` (and related files) so `pthread_join()` on a detached or already-joined thread returns `EINVAL` (POSIX requirement) instead of `ESRCH`.  
**Date:** 2026-04-25

| Model               | Exit Status    | Steps | Patch Correct | Notes                                                              |
|---------------------|----------------|-------|---------------|--------------------------------------------------------------------|
| openai/gpt-5.4      | Submitted      | 12    | Yes           | Changed ESRCH → EINVAL in two spots in `pthread_findjoininfo.c`. Clean minimal fix. |
| anthropic/claude-opus-4-6 | Submitted | 21   | Yes           | Same findjoininfo fix + preserved join info in `completejoin.c` + fixed `detach.c`. Most thorough. |
| gemini/gemini-2.5-pro | LimitsExceeded | 49  | No            | Never submitted. Spiraled into restructuring the pthread implementation — dummy functions, prototype rewrites, 1288-line patch of unrelated changes. |

## Verdict

- **GPT-5.4**: correct and efficient (12 steps).
- **Claude Opus 4.6**: correct and most complete (also handles detach/completejoin edge cases).
- **Gemini 2.5 Pro**: hit step limit without submitting; went entirely in the wrong direction on a complex multi-file task.
