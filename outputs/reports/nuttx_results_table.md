# EmbedEval — NuttX Run Results

**Date:** 2026-04-26
**Models:** GPT-5.4, Claude Opus 4.6, Gemini 2.5 Pro, Qwen3-Coder-480B, DeepSeek-V3
**Step limit:** 50 per run

Pass = agent submitted a patch that was verified by `run_tests` to PASS.
Fail = anything else (wrong patch, never submitted, hit step limit, etc.).

## Per-PR metadata

| PR | Origin | Line diff (kernel PR) | Type |
|----|--------|-----------------------|------|
| #14152 | NuttX  | 3 files, +23 / -4    | Bug fix (`setjmp`/`longjmp` asm — POSIX compliance) |
| #8832  | NuttX  | 4 files, +28 / -16   | Bug fix (`sched/wqueue` semaphore desync) |
| #11889 | NuttX  | 1 file,  +6  / -1    | Bug fix (`memmem()` boundary + zero-length needle) |
| #11898 | NuttX  | 17 files, +302 / -472 | Refactor (`pthread_join` overhaul) |
| #12802 | NuttX  | 14 files, +1031 / -0 | Feature / Kernel addition (`nxevent` event group API) |
| #8885  | NuttX  | 32 files, +500 / -92 | Feature enhancement (widen `sigset_t` to 64 signals) |

## Results

| Model              | PR     | Result | Steps | Origin | Line diff       | Type                      |
|--------------------|--------|--------|-------|--------|-----------------|---------------------------|
| GPT-5.4            | #14152 | Pass   | 3     | NuttX  | 3f, +23/-4      | Bug fix                   |
| GPT-5.4            | #8832  | Pass   | 4     | NuttX  | 4f, +28/-16     | Bug fix                   |
| GPT-5.4            | #11889 | Pass   | 2     | NuttX  | 1f, +6/-1       | Bug fix                   |
| GPT-5.4            | #11898 | Pass   | 12    | NuttX  | 17f, +302/-472  | Refactor                  |
| GPT-5.4            | #12802 | Fail   | 28    | NuttX  | 14f, +1031/-0   | Feature / Kernel addition |
| GPT-5.4            | #8885  | Fail   | 35    | NuttX  | 32f, +500/-92   | Feature enhancement       |
| Claude Opus 4.6    | #14152 | Fail   | 15    | NuttX  | 3f, +23/-4      | Bug fix                   |
| Claude Opus 4.6    | #8832  | Pass   | 13    | NuttX  | 4f, +28/-16     | Bug fix                   |
| Claude Opus 4.6    | #11889 | Pass   | 8     | NuttX  | 1f, +6/-1       | Bug fix                   |
| Claude Opus 4.6    | #11898 | Pass   | 21    | NuttX  | 17f, +302/-472  | Refactor                  |
| Claude Opus 4.6    | #12802 | Fail   | 50    | NuttX  | 14f, +1031/-0   | Feature / Kernel addition |
| Claude Opus 4.6    | #8885  | Fail   | 50    | NuttX  | 32f, +500/-92   | Feature enhancement       |
| Gemini 2.5 Pro     | #14152 | Fail   | 14    | NuttX  | 3f, +23/-4      | Bug fix                   |
| Gemini 2.5 Pro     | #8832  | Fail   | 42    | NuttX  | 4f, +28/-16     | Bug fix                   |
| Gemini 2.5 Pro     | #11889 | Pass   | 12    | NuttX  | 1f, +6/-1       | Bug fix                   |
| Gemini 2.5 Pro     | #11898 | Fail   | 49    | NuttX  | 17f, +302/-472  | Refactor                  |
| Gemini 2.5 Pro     | #12802 | Fail   | 49    | NuttX  | 14f, +1031/-0   | Feature / Kernel addition |
| Gemini 2.5 Pro     | #8885  | Fail   | 50    | NuttX  | 32f, +500/-92   | Feature enhancement       |
| Qwen3-Coder-480B   | #14152 | Pass   | 17    | NuttX  | 3f, +23/-4      | Bug fix                   |
| Qwen3-Coder-480B   | #8832  | Fail   | 37    | NuttX  | 4f, +28/-16     | Bug fix                   |
| Qwen3-Coder-480B   | #11889 | Fail   | 33    | NuttX  | 1f, +6/-1       | Bug fix                   |
| Qwen3-Coder-480B   | #11898 | Fail   | 50    | NuttX  | 17f, +302/-472  | Refactor                  |
| Qwen3-Coder-480B   | #12802 | Fail   | 50    | NuttX  | 14f, +1031/-0   | Feature / Kernel addition |
| Qwen3-Coder-480B   | #8885  | Fail   | 50    | NuttX  | 32f, +500/-92   | Feature enhancement       |
| DeepSeek-V3        | #14152 | Fail   | 49    | NuttX  | 3f, +23/-4      | Bug fix                   |
| DeepSeek-V3        | #8832  | Pass   | 28    | NuttX  | 4f, +28/-16     | Bug fix                   |
| DeepSeek-V3        | #11889 | Fail   | 8     | NuttX  | 1f, +6/-1       | Bug fix                   |
| DeepSeek-V3        | #11898 | Fail   | 49    | NuttX  | 17f, +302/-472  | Refactor                  |
| DeepSeek-V3        | #12802 | Fail   | 50    | NuttX  | 14f, +1031/-0   | Feature / Kernel addition |
| DeepSeek-V3        | #8885  | Fail   | 49    | NuttX  | 32f, +500/-92   | Feature enhancement       |

## Score Summary

| Model            | Pass | Fail | Pass rate |
|------------------|------|------|-----------|
| GPT-5.4          | 4    | 2    | 67%       |
| Claude Opus 4.6  | 3    | 3    | 50%       |
| Gemini 2.5 Pro   | 1    | 5    | 17%       |
| Qwen3-Coder-480B | 1    | 5    | 17%       |
| DeepSeek-V3      | 1    | 5    | 17%       |
