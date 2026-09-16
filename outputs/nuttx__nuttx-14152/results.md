# nuttx__nuttx-14152 — setjmp: fix longjmp(buf, 0) returning 0 instead of 1

**Task:** Fix `arch_setjmp_x86_64.S` and `arch_setjmp_x86.S` so `longjmp(buf, 0)` causes `setjmp` to return 1 (POSIX requirement).  
**Date:** 2026-04-25

| Model               | Exit Status    | Steps | Patch Correct | Notes                                                  |
|---------------------|----------------|-------|---------------|--------------------------------------------------------|
| openai/gpt-5.4      | Submitted      | 3     | Yes           | Fixed x86 + x86_64. Clean minimal fix.                |
| anthropic/claude-opus-4-6 | LimitsExceeded | 15 | Yes        | Fixed x86 + x86_64 + ARM. Most thorough, hit step limit before submitting. |
| gemini/gemini-2.5-pro | Submitted    | 14    | No            | Broken AT&T asm: `%%` instead of `%`, missing `$1` immediates, duplicate labels. Would not assemble. |

## Verdict

- **GPT-5.4**: correct and efficient (3 steps).
- **Claude Opus 4.6**: most complete fix (also patched ARM), but needs a higher step limit to submit.
- **Gemini 2.5 Pro**: over-confidently submitted a broken patch without verifying the build succeeded.
