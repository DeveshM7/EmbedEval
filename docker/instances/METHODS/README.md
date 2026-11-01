# Implementing Method 1 and Method 2

Method 1 = Option 1: inject a distinct realistic bug caught by the unchanged existing regression. Method 2 = Option 2: define a related behavior and minimally adapt or add a regression. Prefer Method 1. A changed regression always makes the instance Method 2, even when most test code is reused.

## 1. Inspect the validated parent

Read the issue/problem statement, pinned buggy and fixed revisions, complete upstream patch, regression tests, controls, source surrounding the changed code, build definitions, dependencies, and runner. Confirm which tests are upstream and which were authored for the benchmark. Reproduce the original failure and the fixed pass before accepting a new instance. Preserve the original environment; a replacement toy implementation is insufficient.

Write the original mechanism as a causal chain: trigger → incorrect state or operation → observable failure. Describe the violated invariant rather than just the edited line. Do not infer correctness from compilation alone.

## 2. Define the candidate before injecting it

Create an `oracle.json` or dated plan containing the intended behavior, original mechanism, proposed mechanism, distinction, realistic developer mistake, expected failing tests, expected passing controls, and exact assertions that should fail. Start from the correct upstream implementation. Preserve the original fix.

For Method 1, inspect whether existing assertions cover another invariant on the same execution path. Do not change test inputs, assertions, fixtures, runner, timeout settings, or expected outcomes to accommodate the mutation. Reject simple reversions, equivalent rewrites of the original fault, syntax errors, hardcoded test cases, and bugs detected only by controls rather than the existing regression.

Example: OpenThread #13494 originally misses reader output arriving after the virtual-time budget and falsely times out. The Method 1 `openthread-13494-late-order` mutation keeps bounded collection but prepends late lines, reversing response order. Existing ping and diagnostic list-equality assertions detect reversal. Single-line completion and timeout controls continue passing. This is a queue-merge ordering mistake, with a short repair and consequently low expected difficulty.

## 3. Use Method 2 only when justified

Explain concretely why the unchanged regression cannot convincingly detect a distinct realistic bug. Define the new expected behavior and oracle before changing production code. Extend the existing regression minimally, using the same real implementation and environment; create a separate test only when necessary. Cover a meaningful state transition, boundary, or related behavior without encoding the intended repair.

Example: `openthread-13494-multi-digit-error` extends the delayed-error test to a multi-digit error code. The candidate incorrectly recognizes only single-digit errors. Existing single-digit cases cannot distinguish this behavior; the added test must recognize the full error response without timing out. See its instance report and patches for the precise implementation and evidence.

## 4. Validate the causal claim

Use an isolated checkout or container. Keep gold repairs and reports outside the agent-visible build context. Save complete commands, runtime/image identities, exit statuses, per-test results, and stdout/stderr for every phase.

| Phase | Required result |
|---|---|
| Correct production + selected regression and controls | PASS |
| Synthetic production + identical selected regression and controls | Intended regression FAIL; relevant controls PASS |
| Correct production restored + identical tests | PASS |

For Method 1, hash the original regression and runner to prove they are unchanged. For Method 2, keep the synthetic test identical across all three phases and record its minimal diff against the parent. Also run the original suite without the test extension when useful to establish the new behavior's coverage. Check that restoration matches the correct production bytes. A compile error, import error, transport setup error, or unrelated timeout is not an accepted semantic failure. Repeat timing-sensitive cases when needed.

Run from the exported bundle root with Python 3.10+ and a working Linux Docker engine:

```sh
python reproduce.py --list
python reproduce.py --instance openthread-13494-late-order --build --validate
python reproduce.py --instance openthread-13494-multi-digit-error --build --validate
```

The validator starts from the packaged buggy image, applies its repair for the correct phase, reverses that repair for the synthetic phase, then reapplies it for restoration. It checks exact test sets, expected failures, exit statuses, unchanged test/runner hashes, and source restoration. New logs go to `validation/<instance>/`. A direct buggy `run_tests` exits 1 by design; that alone does not validate an instance. If Docker is unavailable, record that limit explicitly rather than claiming a container pass.

## 5. Package and label every accepted instance

Use a unique local synthetic ID; do not invent an upstream PR number. Add `method_label: "Method 1"` or `"Method 2"` to the manifest and display that label in the instance README and catalog. The existing `method: "Option 1"`/`"Option 2"` field remains for compatibility. These are artifact labels; they do not change upstream GitHub PR labels.

Include the parent provenance and problem description, bug patch, minimal test patch for Method 2, repair patch, build context, expected outcomes, logs, and report. Method 1 must have no logical test changes. Keep repairs outside the image's build context. If distributing a ZIP, regenerate it after changing labels or documentation and refresh any checksum inventory.

The report must state: method, original and synthetic mechanisms, why distinct, files and tests changed, exact test commands, phase PASS/FAIL counts, exact assertion/failure reason, control results, realism, similarity, and validity concerns. Distinguish new measurements from historical evidence. Mention focused mocks, untested integrations, dependency drift, scheduling limits, and low difficulty where applicable. Do not publish a candidate that cannot meet the semantic oracle cleanly; record its rejection instead.
