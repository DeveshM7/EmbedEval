# nuttx__nuttx-8885 — signal: widen sigset_t from uint32_t to uint32_t[2]

**Task:** Convert `sigset_t` from a scalar `uint32_t` to a two-element array across 30 files, adding `SIGSET_FMT`/`SIGSET_ELEM()` macros and a `sigset_isequal()` helper so the updated ostest `sighand.c`/`sighelper.c` compile and pass.  
**Date:** 2026-04-25

| Model               | Exit Status    | Steps | Patch Correct | Notes                                                                  |
|---------------------|----------------|-------|---------------|------------------------------------------------------------------------|
| openai/gpt-5.4      | Submitted      | 35    | No            | Edited `include/signal.h` but introduced struct member access errors. Cascading build failures across drivers, fs, procfs. Submitted despite build still broken. |
| anthropic/claude-opus-4-6 | LimitsExceeded | 50  | No          | Correctly identified the array conversion needed; partially updated `include/signal.h` and apps files. Hit step limit before achieving a clean build. |
| gemini/gemini-2.5-pro | LimitsExceeded | 50  | No            | 941-line patch; still hitting sched build errors at step limit. Never reached a passing build. |

## Verdict

**0/3** — No model completed this refactor. 30-file type-widening across the entire signal subsystem is beyond what any model could coordinate within 50 steps. Claude made the most structural progress (correct approach, partial conversion) but ran out of budget. GPT submitted broken code; Gemini accumulated the most changes but still had build errors.
