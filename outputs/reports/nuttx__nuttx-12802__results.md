# nuttx__nuttx-12802 — sched/nxevent: add kernel event group API

**Task:** Implement the full `sched/event/` subsystem (`event_init`, `event_post`, `event_wait`, `event_reset`, `event_destroy`) plus `include/nuttx/event.h`, Kconfig, and Makefile wiring so `nxevent_test` in ostest compiles and passes.  
**Date:** 2026-04-25

| Model               | Exit Status    | Steps | Patch Correct | Notes                                                                  |
|---------------------|----------------|-------|---------------|------------------------------------------------------------------------|
| openai/gpt-5.4      | Submitted      | 28    | No            | Scaffolding only (Kconfig + Makefile + defconfig). Event implementation wrong — nxevent_test hung every run_tests attempt. Submitted despite repeated 300s timeouts. |
| anthropic/claude-opus-4-6 | LimitsExceeded | 50  | No          | Same scaffolding captured in patch; actual event files created as untracked new files. Implementation incorrect — nxevent_wait blocked indefinitely. Hit step limit. |
| gemini/gemini-2.5-pro | LimitsExceeded | 49  | No            | Scaffolding only in patch. Implementation incorrect. Hit step limit.   |

## Verdict

**0/3** — No model produced a working event API implementation. This is a feature-addition PR requiring ~10 new files from scratch. All models correctly identified the scaffolding (Kconfig, Makefile, defconfig) but could not implement correct `nxevent_wait` semantics within the step budget. The 300s timeouts seen by all models indicate a hanging `nxevent_wait` — a deadlock in the event posting/waiting logic.
