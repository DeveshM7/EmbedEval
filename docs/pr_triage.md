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

### This is not restricted to bug fixes

A **new feature or a new driver is a perfectly good task** — the agent simply
has to implement it until the tests pass, rather than repair something. What
matters is only that the tests fail before the change and pass after it.

So do not reject a PR for being an addition rather than a correction. Judge it
on whether its tests reliably verify what it does.

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

Read all of it. You are being used here precisely because you can read the
whole thing and judge causality; nothing has been summarised or trimmed for
you. Review comments deserve particular attention — a reviewer saying a fix
misses the root cause is the highest-signal content in a PR, and it appears
nowhere else.

---

## Accept when all of these hold

1. **The source change alters behaviour.** Either a correction — an off-by-one,
   a bad bounds check, a wrong error code, an ordering or locking mistake — or
   an addition, such as a new API, subsystem, or driver.
2. **The test change exercises that specific behaviour.** You should be able to
   point at the assertion that would fail if the source change were removed.
3. **The test lives under `tests/` and looks runnable in an emulator** — that
   is, it does not depend on a physical board, a sensor, a radio, or an
   external network service.
4. **The failure is deterministic.** It fails every run, not one in twenty.

---

## Reject when any of these hold

- **Requires real hardware** to run or verify. This is the most common reason a
  new driver or peripheral PR has to be rejected — and note that the reason is
  the hardware, never the fact that it adds something new.
- **Pure refactor.** Behaviour is unchanged by design, so no test can tell
  before from after.
- **Documentation, formatting, typo, or comment-only change.**
- **Version bump or dependency update.**
- **Test-only change** with no behavioural source change.
- **Flaky-test fix** — raising a timeout, adding a retry, loosening a
  tolerance. The test changed because it was wrong, not because the code was.
- **The source change and the test are unrelated** — both present but not
  causally connected. Common in large PRs that bundle several changes.

---

## These are NOT criteria

Each of the following looks like a sensible rule and was measured against the
eight Zephyr PRs already hand-validated as good instances. Each one would have
thrown good instances away. Do not use them.

| Tempting rule | Rejects |
|---|---|
| "must add a new test file" | 5 of 8 — most fixes add cases to an existing file |
| "must have the `bug` label" | 4 of 8 |
| "must be a small diff" | PR 74435 is a good instance with 21 files |
| "must have a linked issue" | PR 43405 has none |

A linked issue (`Fixes #NNNN`) is a genuine *positive* signal — 7 of the 8 have
one — but its absence is not disqualifying.

Likewise, size is not a criterion in either direction. A one-file fix and a
sixteen-file change are both acceptable if the test detects the change.

---

## Output

Return exactly this JSON. No prose outside it.

```json
{
  "pr": 65697,
  "verdict": "accept",
  "confidence": "high",
  "change_type": "fix",
  "change": "pthread_key_delete() always deletes the key at index 0 rather than the key matching its argument.",
  "detecting_test": "tests/posix/common/src/key.c — test_correct_key_is_deleted asserts the deleted key equals the requested one.",
  "problem_statement": "...",
  "reason": "Source change corrects the index used when freeing the key; the added assertion fails without it."
}
```

Fields:

- `verdict` — `accept` or `reject`
- `confidence` — `high`, `medium`, or `low`
- `change_type` — `fix` or `feature`. Both are acceptable; this is recorded so
  we can see the mix in the problem set, **not** used to reject.
- `change` — one sentence on what the PR changes: the wrong behaviour it
  corrects, or the capability it adds. Empty if rejecting.
- `detecting_test` — the file, and the specific assertion that catches it.
  If you cannot name one, the verdict is `reject`.
- `problem_statement` — what the agent will be shown. For a fix, describe the
  **symptom and expected behaviour**. For a feature, describe the **capability
  required and how it should behave**. Never name the implementation, the
  function to write or change, or the line to edit; the agent has to find those.
- `reason` — one or two sentences justifying the verdict.

When uncertain, prefer `accept` with `"confidence": "low"`. A wrong accept is
caught downstream when the instance fails to validate and costs one build. A
wrong reject is never revisited and the PR is lost silently.

---

## Worked examples

**PR 65697 — accept.** One source file, one test file. `pthread_key_delete()`
frees the wrong key; the test asserts the correct key was deleted. Small,
causally tight, deterministic, runs on `qemu_x86`.

**PR 74435 — accept.** Twenty-one files, sixteen of them source. Large, but the
RTIO test changes assert exactly the behaviour the source changes correct. Size
is not a reason to reject.

**A PR adding a new subsystem with tests that run on `native_sim` — accept.**
`change_type` is `feature`. The agent has to implement the subsystem until
those tests pass. That is a legitimate task, and often a harder one than a
fix. Being an addition is not a reason to reject.

**A PR adding a driver for a physical sensor over I2C — reject.** Not because
it is a feature, but because the tests need the actual part on a real board.
If its tests run against an emulated bus or a mock and genuinely verify the
driver's behaviour, accept it.

**PR 33690 — accept, and note what it is not.** Thirteen files and *no new test
file* — it modifies existing sensor tests. It carries no `bug` label. Both of
those are fine.

**A PR that adds a new driver plus tests for it — reject.** The tests are new
capability coverage, not bug detection. There is no prior wrong behaviour for
an agent to correct.
