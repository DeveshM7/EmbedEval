# Zephyr PR triage

Instructions for the model that decides which merged Zephyr PRs can become
EmbedEval benchmark instances.

This runs *after* the hard filters in `scripts/filter_candidates.py`, so every
PR you see is already merged, already touches `tests/`, and already touches
source outside `tests/`. Do not re-check those.

---

## What you are deciding

A benchmark instance gives a coding agent the Zephyr repository at the commit
**before** a PR landed, plus the tests that PR added or changed. The agent has
to make those tests pass without seeing the PR's own changes.

For that to work, the PR must satisfy **both** halves of one question:

> **Does this PR make a behavioural change, and do its tests reliably detect
> whether that change is present?**

Either half alone is useless. A change with no detecting test gives the agent
nothing to aim at. A test change with no behavioural change gives it nothing to
solve.

---

## What you are given

One enriched record per PR, produced by
`scripts/filter_candidates.py enrich`, carrying the same substance as the
GitHub page:

- Title, body, labels, and the merge commit
- **The complete diff**, per file — source and tests
- **Linked issues** (`Fixes #NNNN`) with their body and comments, which is
  usually the bug report in the reporter's own words
- **Commit messages** from the PR
- **The discussion thread** and **inline review comments**, including the
  diff hunk each review comment is attached to
- **The complete test suite** behind every test file the PR touched — every
  file in that directory, in full, not just the changed lines

Read all of it. You are being used here precisely because you can read the
whole thing and judge causality; nothing has been summarised or trimmed for
you.

---

## What we can run

This is the whole list. Anything outside it we cannot execute today.

| platform | what it is |
|---|---|
| `native_sim`, `native_sim_64`, `native_sim/native/64` | Zephyr compiled as a native Linux binary and run as an ordinary process. No CPU emulation. |
| `qemu_x86` | An emulated x86 machine, run under the QEMU bundled in the Zephyr SDK. |

Not available, and a reason to reject on their own:

- Other QEMU targets — `qemu_cortex_m3`, `qemu_riscv32`, `qemu_xtensa` and the
  rest.
- Other simulators — `renode`, `bsim`/babblesim, `armfvp`.
- Any physical board.

Results are read from **ztest console output**, so the suite must report that
way. A `pytest`, `bsim`, `robot` or `shell` harness produces its verdict
somewhere we never see, and cannot be evaluated at all.

---

## Accept when all of these hold

1. **You can name a test in the test diff that would fail without the source
   change.** This is the core requirement, and it is deliberately concrete: you
   are not asked to judge whether the change is significant or meaningful, only
   to point at the test that detects it.

   How the test fails does not matter. A failed assertion is the common case,
   but a timeout, a hang, a crash, an assertion inside the code under test, or
   a non-zero exit all count equally.
2. **The suite runs on something from the list above.** Judge this from the
   suite's config file, which is included — `testcase.yaml`, `tests.yaml`, or
   `sample.yaml`:

   - `platform_allow` present → at least one entry must be `native_sim` (any
     variant) or `qemu_x86`. If it lists only other targets, reject.
   - `platform_exclude` → reject only if it excludes both of ours.
   - `harness:` present and not `ztest` or `console` → reject.
   - `depends_on` → judge against what those two platforms provide. Note that
     `native_sim` does have working networking, so `depends_on: netif` is
     fine; a real sensor, radio, or USB device is not.
   - No config file, or nothing conclusive → judge from the test sources,
     which you also have. When still unsure, accept with low confidence rather
     than reject.
3. **The failure is deterministic.** It fails every run, not one in twenty.

---

## Reject when any of these hold

- **Requires real hardware** to run or verify — a physical board, a sensor, a
  radio, an external network service.
- **Uses a harness we cannot read** — anything other than `ztest` or
  `console`.
- **Pure refactor.** Behaviour is unchanged by design, so no test can tell
  before from after.
- **Documentation, formatting, typo, or comment-only change.**
- **Version bump or dependency update.**
- **Test-only change** with no behavioural source change.
- **Flaky-test fix** — raising a timeout, adding a retry, loosening a
  tolerance. The test changed because it was wrong, not because the code was.

---

## Output

Return exactly this JSON. No prose outside it.

```json
{
  "pr": 65697,
  "verdict": "accept",
  "confidence": "high",
  "change_type": "fix",
  "platform": "qemu_x86",
  "extra_configs": [],
  "problem_statement": "pthread_key_delete() deletes the wrong key. Rather than deleting the key supplied as its argument, it always deletes the key at index 0. Creating and deleting keys in a loop therefore exhausts the key pool, because the wrong key is freed each time.",
  "fail_to_pass": ["test_correct_key_is_deleted", "test_key_resource_leak"],
  "pass_to_pass": ["test_key_1to1_thread", "test_key_Nto1_thread"],
  "detecting_test": "tests/posix/common/src/key.c — test_correct_key_is_deleted checks the deleted key equals the requested one; it fails without the source change.",
  "reason": "The source change corrects the index used when freeing the key, and the test checks exactly that."
}
```

These fields are written straight into the instance, so their format matters
more than their prose.

- `verdict` — `accept` or `reject`. Everything below may be omitted when
  rejecting, except `reason`.
- `confidence` — `high`, `medium`, or `low`.
- `change_type` — `fix` or `feature`. Recorded so we can see the mix in the
  problem set; never a reason to reject.
- `platform` — **exactly one** platform string from the list above, the one
  the suite should be built and run with. This becomes the build command, so
  it must be a real board name: `native_sim`, `native_sim/native/64`, or
  `qemu_x86`. Prefer what the suite's config points to — `integration_platforms`
  first, then `platform_allow`. If both are absent and either would work,
  choose `native_sim`, which is faster.
- `extra_configs` — Kconfig settings the test needs in order to detect the
  change, as a list like `["CONFIG_RTIO_SUBMIT_SEM=n"]`. Usually empty.
  Fill it when the suite's config file defines scenarios with `extra_configs`
  and the test you nominated only works under one of them — take that
  scenario's settings. This matters because the build reads `prj.conf` alone
  and ignores scenarios, so a test built in the wrong variant passes on
  unfixed code and the instance is discarded as useless.
- `fail_to_pass` — the ztest test **identifiers** that fail before the change
  and pass after, as a list, e.g. `["test_correct_key_is_deleted"]`. Names as
  they appear in `ZTEST(...)` in the source, not descriptions. If you cannot
  name at least one, the verdict is `reject`.
- `pass_to_pass` — identifiers of other tests in the same suite that pass
  before and must still pass after. These catch a fix that breaks something
  else. Name a few real ones from the suite; an empty list is acceptable if
  the suite has no other tests.
- `problem_statement` — what the agent is shown, and the one field where
  wording matters. Describe the **symptom and the expected behaviour** for a
  fix, or the **capability required and how it should behave** for a feature.
  Never name the implementation, the function to write or change, or the line
  to edit — finding those is the task.
- `detecting_test` — one sentence naming the test and how it fails without the
  change. This is your justification for `fail_to_pass`, and it is read by
  humans, not parsed.
- `reason` — one or two sentences justifying the verdict.

When uncertain, prefer `accept` with `"confidence": "low"`. A wrong accept is
caught downstream when the instance fails to validate and costs one build. A
wrong reject is never revisited and the PR is lost silently.

---

## Worked examples

**PR 65697 — accept.** One source file, one test file. `pthread_key_delete()`
frees the wrong key; `test_correct_key_is_deleted` checks that the deleted key
equals the requested one. Deterministic, runs on `qemu_x86`.

**PR 74435 — accept.** Twenty-one files, sixteen of them source. The RTIO test
changes exercise exactly the behaviour the source changes alter.

**PR 33690 — accept.** Modifies existing sensor tests rather than adding a new
test file; the existing tests still detect the change.

**A new subsystem with tests that run on `native_sim` — accept.**
`change_type` is `feature`. The agent implements the subsystem until the tests
pass.

**A driver for a physical sensor over I2C — reject.** The tests need the actual
part on a real board. If instead they run against an emulated bus or a mock and
genuinely verify the driver's behaviour, accept it.
