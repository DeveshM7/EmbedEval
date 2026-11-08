# RIOT PR triage

Instructions for the model that decides which merged RIOT OS PRs become EmbedEval
benchmark instances, and writes what the coding agent is told.

## What you are deciding

An instance gives a coding agent the RIOT repository at the commit before a PR
landed, plus the tests that PR added or changed. The agent has to make those tests
pass without seeing the PR's own changes. It is told only your `problem_statement`.

Scripts have already proved the mechanics. Do not re-check them: the tests named in
the record were built and run on RIOT's `native` board, without and with the PR's
change. They fail or do not compile without it, and pass with it. **That they went
from failing to passing is settled. It does not mean the PR is a good task.** Your
job is what no script can tell:

> **Is there a real change in behaviour or capability here, that a capable engineer
> could implement from a description, and do the tests check that change?**

## What you are given

The PR's title, description, labels, commit messages, discussion and linked issues;
the full diff of the source change and of the tests; and what happened when the
tests ran:

- **strong**: the test built and failed without the change, and passed with it.
- **weak**: the test did not compile or link without the change, and passed with it.
  The compile error is shown. Every test in that target counts as failing before,
  so most listed tests may have nothing to do with the change.

## Reject when any of these holds

Reject for a concrete reason you can name:

- **`no_behaviour_change`**: a refactor, rename, move, type or signature cleanup,
  or an API reshuffle. Behaviour is the same by design. This holds even though the
  tests fail without it: tests adapted to a renamed function or a new signature do
  not detect any behaviour.
- **`build_only`**: the change is to the build system, compiler compatibility,
  Makefiles or dependency versions, and the tests fail only because they were
  adapted to it.
- **`tests_do_not_check_it`**: the tests pass or fail for a reason unrelated to the
  source change, or only check something incidental such as the order of a listing.
- **`not_describable`**: the change is so large, or so arbitrary, that no description
  short of the diff would let an engineer make the tests pass. A new API is not
  arbitrary if the tests show how it is called and you can describe what it does.
- **`test_only`**: nearly all the source change is incidental, and the PR mostly adds
  tests for code that already worked.

Keep the balance. A feature is a perfectly good task: new functions the tests call
are fine, as long as you can say what they must do. Reject only for a reason above.
When you are unsure, accept with `"confidence": "low"` and say what you were unsure of.

## The problem statement

The one field where wording matters; it goes to the agent word for word.

- For a bug: the **symptom and the expected behaviour**, as a user of the code sees it.
- For a feature: the **capability required and how it should behave**.
- Name a function, type, macro, module or file only if **the tests use that exact
  name**: the agent cannot guess it, and the tests will not compile otherwise. For
  each such name, give its signature or use as the tests show it. Name nothing else
  that the change adds or edits.
- **Say what, never how.** State what a caller of the code observes and must be able
  to do. Finding how to get there is the task. So never:
  - say how existing code must change, or how one function should use another:
    not "modify X to call Y", not "X and Y must call these internally";
  - give the cause inside the code: not "the offset is wrong", not "it omits the
    extra bytes", not "the length is only added when ...";
  - describe an algorithm, a data structure or an internal field, unless the tests
    themselves read that field.
- For a bug, the test of a good statement: it could have been written by someone who
  saw the wrong behaviour from outside and never read the code.
- Never mention the PR, its tests by file, or this benchmark.
- Plain prose, a few sentences. Precise enough to be solvable; no longer than needed.

Written as it must not be, then as it must:

> `foo_parse()` skips a field when it ignores a record, so it reads the next record
> at the wrong offset. Modify `foo_parse()` to call `foo_skip()` for ignored records.

> `foo_parse()` must return the address from a reply in which records it ignores come
> before the matching record. Today it fails on such replies.

## Output

Return exactly this JSON, with no prose outside it:

```json
{
  "pr": 20857,
  "verdict": "accept",
  "reject_reason": null,
  "confidence": "high",
  "change_type": "fix",
  "problem_statement": "Parsing a DNS reply fails when the reply contains a record of a type the parser skips ...",
  "reason": "A real parsing bug; the new test feeds in such a reply and checks the result."
}
```

- `verdict`: `accept` or `reject`.
- `reject_reason`: when rejecting, one of the five names above; otherwise null.
- `confidence`: `high`, `medium` or `low`.
- `change_type`: `fix` or `feature`. A fix corrects behaviour that was wrong; a
  feature adds something new. Decide from the change, not from the PR's labels,
  which are often missing or wrong. Never a reason to reject.
- `problem_statement`: required when accepting. Code checks it: a name that the source
  change touches and the tests never use, or wording that says how to change the code,
  comes back to you with the reason.
- `reason`: one or two sentences.
