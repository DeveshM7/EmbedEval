# Mynewt Phase 1 status (for alex)

Worktree `/Users/alexz/Desktop/VIP/EmbedEval-mynewt`, branch
`feature/mynewt-triage` (from `origin/feature/mynewt-pr-filtering`, no upstream
set, nothing pushed). Scholar copy: `~/EmbedEval-mynewt` (venv is a symlink to
`~/EmbedEval/.venv`; mynewt-core bare clone in `/scratch/scholar/zhan5136/repos/mynewt-core.git`).

## A. Instances: done

- The three instances were hand-made. With your approval they were replaced
  by generator output from `triage/mynewt_reviewed/<pr>.json`, after each
  validated **3/3**, run one at a time (9/9 VALID, no Rosetta flakes). Each
  "before" run failed exactly the `fail_to_pass` cases with the rest of the
  inventory passing, so the failure comes from the changed test.
- Hand-made versions: `archive/mynewt-handmade-instances/` (README lists every
  difference); their images are kept as `embedeval:mynewt-<pr>-handmade`.
- Runner can't be fooled: a missing, unexpected or crashed testcase returns 3
  (covered by `tests/test_mynewt_runner.py`), and the validator requires the
  exact pass/fail sets. `run_tests` rebuilds from a clean `/project/bin` every
  time, so no stale-build issue; compiler flags live in the Dockerfile via
  metadata, not in a validation script.

## B. Gold set and runner: done

- `triage/mynewt_gold.json`: 7 model-scored cases (accept 2809, 3299, 3680;
  reject 2157, 3509, 1702, 3381). 3708, 3707, 2511, 821 and 251 are
  `hard_filter_reject`: the filters drop them before the model, checked by
  `filter_candidates.py selftest --repo mynewt`.
- **Note:** #251 turned out to be a hard-filter reject too (its test is under
  `encoding/cborattr/test/`, the pre-`selftest` layout), so it isn't scored.
- **Note:** 3509, 1702 and 3381 have no linked issue. The checker mirrors
  `generate_instance.py`, which refuses Mynewt PRs without one, so an accept
  of these gets a repair message. Only 2157 tests reject-side judgment. If you
  want harder rejects, candidates need a linked issue *and* a selftest change.
- `scripts/triage_mynewt_prs.py` (+ `tests/test_mynewt_triage.py`).

## C/D. Scholar runs and the improvement loop

**Target met with the unchanged prompt: 7/7 on 3 runs of the same prompt, no
errors.** No prompt round was needed; the only prompt edits were removing the
gold answers from its examples and describing the tools, both before run 1.
Run folders are local (gitignored) under `triage/mynewt_runs/`.

Prompt under test: `docs/mynewt_pr_triage.md` at commit 702255f
(prompt sha256 prefix `29fd484fe820d485`), model `qwen38-nvfp4` on my own
server job (474610). Job 474606 was already running and is not mine (likely the NuttX session); left alone.

| Run | Folder | Score | Repairs | Errors |
|---|---|---|---|---|
| 1 | `triage/mynewt_runs/2026-10-12_121556_qwen38-nvfp4` | 7/7 | 0 | 0 |
| 2 | `triage/mynewt_runs/2026-10-12_124856_qwen38-nvfp4` | 7/7 | 0 | 0 |
| 3 | `triage/mynewt_runs/2026-10-12_130446_qwen38-nvfp4` | 7/7 | 1 (3381, parse) | 0 |

Run 1 notes (not verdict misses, but they matter for Phase 2 generation):
- 2809: the model's `compatibility_cflags` is `[]`; the instance needs
  `-Wno-error=stringop-overflow`, which only a build with today's compiler
  reveals. Expect to add such flags at validation time, not from triage.
- 3299: the model accepted without `test_commit`/`test_support_paths`, so an
  instance generated from its verdict would miss the SELFTEST-only accessor the
  new tests call and fail to compile. Accept verdicts should be treated like
  NuttX's `needs_reviewed_recipe` until a validator run confirms them.
- Slowest case: 3680 at ~32 min (7 turns, 29K completion tokens).
- Run 2 set the correct `test_commit` for 3299 (run 1 did not), so the
  model is inconsistent on that field, not unable.
- Run 3, 3381: the first reply had prose before the JSON, and the prose
  quoted `= {}`, so `parse_verdict` (first `{` to last `}`) failed; the repair
  turn fixed it. That is a checker weakness, not a prompt one (see below).
