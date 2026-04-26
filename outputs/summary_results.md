# EmbedEval — Summary Results

**Date:** 2026-04-26  
**Models:** GPT-5.4, Claude Opus 4.6, Gemini 2.5 Pro, Qwen3-Coder-480B, DeepSeek-V3  
**Instances:** 6 NuttX kernel bug-fix PRs

---

## Legend

| Symbol | Meaning |
|--------|---------|
| ✓ | Submitted with correct patch, verified by run_tests PASSED |
| ~ | Correct patch, run_tests PASSED mid-run, but did NOT submit within step limit |
| ? | Submitted but run_tests never PASSED (patch may be logically correct but unverified) |
| ✗ | Failed — wrong patch, or run_tests never PASSED |

---

## Results by Instance

### nuttx__nuttx-14152 — `longjmp(buf,0)` must return 1 (x86/x86_64 asm fix, 2–3 files)

| Model | Result | Steps | Exit | Notes |
|-------|--------|-------|------|-------|
| GPT-5.4 | ✓ | 3 | Submitted | Clean cmov-based fix for x86 + x86_64. |
| Claude Opus 4.6 | ~ | 15 | LimitsExceeded | Correct fix for x86 + x86_64 + ARM. run_tests PASSED. Hit step limit before submitting. |
| Gemini 2.5 Pro | ✗ | 14 | Submitted | Submitted despite run_tests FAILED every time. Broken AT&T asm (`%%` instead of `%`). |
| Qwen3-Coder-480B | ✓ | 17 | Submitted | Correct cmovzl fix. run_tests PASSED. |
| DeepSeek-V3 | ~ | 49 | LimitsExceeded | Correct fix. run_tests PASSED. Kept going and exhausted step limit without submitting. |

---

### nuttx__nuttx-8832 — Drain all work items per wakeup (1-file fix, `kwork_thread.c`)

| Model | Result | Steps | Exit | Notes |
|-------|--------|-------|------|-------|
| GPT-5.4 | ✓ | 4 | Submitted | `if` → `while` loop. Clean. run_tests PASSED (call=100). |
| Claude Opus 4.6 | ✓ | 13 | Submitted | `while` loop + `nxsem_trywait` for semaphore balance. run_tests PASSED. |
| Gemini 2.5 Pro | ✗ | 42 | LimitsExceeded | Never got run_tests PASSED. Garbled patch. |
| Qwen3-Coder-480B | ? | 37 | Submitted | `do-while` drain loop (logically correct). Never ran `run_tests` properly — submitted unverified. |
| DeepSeek-V3 | ✓ | 28 | Submitted | Clean `while (dq_remfirst != NULL)` loop. run_tests PASSED (call=100). |

---

### nuttx__nuttx-11889 — `memmem()` off-by-one + zero-length needle (1-file fix)

| Model | Result | Steps | Exit | Notes |
|-------|--------|-------|------|-------|
| GPT-5.4 | ✓ | 2 | Submitted | `<` → `<=` + zero-length guard. run_tests PASSED. |
| Claude Opus 4.6 | ✓ | 8 | Submitted | Same fix. run_tests PASSED. |
| Gemini 2.5 Pro | ✓ | 12 | Submitted | Same fix. run_tests PASSED. |
| Qwen3-Coder-480B | ~ | 33 | LimitsExceeded | Correct fix. run_tests PASSED once. Then did `make distclean` + reconfigure, re-enabled login prompt, broke subsequent runs. Never recovered to submit. |
| DeepSeek-V3 | ~ | 8 | LimitsExceeded | Correct fix. run_tests PASSED. Then produced 5 consecutive prose-only responses with no tool call for the submit step — rejected each time, hit step limit. |

---

### nuttx__nuttx-11898 — `pthread_join` on detached thread must return EINVAL (17-file refactor)

| Model | Result | Steps | Exit | Notes |
|-------|--------|-------|------|-------|
| GPT-5.4 | ✓ | 12 | Submitted | `ESRCH` → `EINVAL` in `pthread_findjoininfo.c`. run_tests PASSED. |
| Claude Opus 4.6 | ✓ | 21 | Submitted | Same + preserved join info in `completejoin.c` + fixed `detach.c`. Most thorough. run_tests PASSED. |
| Gemini 2.5 Pro | ✗ | 49 | LimitsExceeded | Never got run_tests PASSED. 1288-line garbled patch — rewrote unrelated prototype files, added dummy functions. |
| Qwen3-Coder-480B | ✗ | 50 | LimitsExceeded | Got run_tests to run but last result was FAILED. Hit step limit. |
| DeepSeek-V3 | ✗ | 49 | LimitsExceeded | Never got run_tests PASSED. |

---

### nuttx__nuttx-12802 — Add `sched/event/` kernel event group API from scratch (10+ new files)

| Model | Result | Steps | Exit | Notes |
|-------|--------|-------|------|-------|
| GPT-5.4 | ✗ | 28 | Submitted | Submitted despite repeated 300s timeouts. Only scaffolding (Kconfig + Makefile + defconfig) in patch; event implementation files untracked. Incorrect `nxevent_wait` — test hung every time. |
| Claude Opus 4.6 | ✗ | 50 | LimitsExceeded | Same scaffolding pattern. `nxevent_wait` never correct. Hit step limit. |
| Gemini 2.5 Pro | ✗ | 49 | LimitsExceeded | Scaffolding only. Hit step limit. |
| Qwen3-Coder-480B | ✗ | 50 | LimitsExceeded | Scaffolding only. Hit step limit. |
| DeepSeek-V3 | ✗ | 50 | LimitsExceeded | Scaffolding only. Hit step limit. |

---

### nuttx__nuttx-8885 — Widen `sigset_t` from `uint32_t` to `uint32_t[2]` (30-file refactor)

| Model | Result | Steps | Exit | Notes |
|-------|--------|-------|------|-------|
| GPT-5.4 | ✗ | 35 | Submitted | Submitted with broken patch — struct member access errors in `include/signal.h`. Build never succeeded. |
| Claude Opus 4.6 | ✗ | 50 | LimitsExceeded | Correct approach (array conversion), partial conversion across some files, never reached clean build. |
| Gemini 2.5 Pro | ✗ | 50 | LimitsExceeded | 941-line patch, still hitting sched build errors at limit. |
| Qwen3-Coder-480B | ✗ | 50 | LimitsExceeded | Incomplete conversion. Hit step limit. |
| DeepSeek-V3 | ✗ | 49 | LimitsExceeded | 319-line patch, never reached clean build. |

---

## Score Summary

| Model | ✓ Pass | ~ Near-miss | ✗ Fail | Pass Rate |
|-------|--------|-------------|--------|-----------|
| GPT-5.4 | 4 (14152, 8832, 11889, 11898) | 0 | 2 | **4/6 (67%)** |
| Claude Opus 4.6 | 3 (8832, 11889, 11898) | 1 (14152) | 2 | **3/6 (50%)** |
| Gemini 2.5 Pro | 1 (11889) | 0 | 5 | **1/6 (17%)** |
| Qwen3-Coder-480B | 1 (14152) | 1 (11889) | 4 | **1/6 (17%)** |
| DeepSeek-V3 | 1 (8832) | 2 (14152, 11889) | 3 | **1/6 (17%)** |

> **Near-misses** (~ column): agent produced a correct, verified fix but failed to submit — due to step limit exhaustion, self-inflicted config breakage (Qwen on 11889), or inability to format the submit command (DeepSeek on 11889).

---

## Observations

**Difficulty ranking (easiest → hardest):** 11889 < 14152 < 8832 < 11898 < 12802 < 8885

**GPT-5.4** is the most reliable — solves targeted single-file and multi-file bug fixes efficiently, submits decisively after run_tests PASSED. Only fails on the two hardest instances (feature addition and 30-file refactor).

**Claude Opus 4.6** is the most thorough — produces the most complete patches (fixes ARM in 14152, adds semaphore handling in 8832). Near-misses on 14152 due to over-engineering; otherwise reliable.

**Gemini 2.5 Pro** succeeds only on the simplest 1-file fix (11889) and over-confidently submits broken patches on harder tasks. Never gets run_tests PASSED on anything requiring assembly or multi-file coordination.

**Qwen3-Coder-480B** solves 14152 correctly and had a correct 11889 fix, but is prone to self-destructive behavior (reconfiguring after a PASSED result, breaking its own environment).

**DeepSeek-V3** produces correct fixes on targeted bugs (8832, 11889, 14152) but struggles to stay within the step budget — repeatedly gets the right answer but can't submit in time or can't format the submit command.

**Common failure modes:**
- Agents keep running commands after run_tests PASSED instead of submitting immediately
- `make distclean` + reconfigure mid-run can re-enable `NSH_CONSOLE_LOGIN`, breaking subsequent test runs
- Feature-addition PRs (12802) and whole-system refactors (8885) are beyond all models at 50 steps
