# Hand-written Mynewt instances (archived 2026-10-12)

These three instances were written by hand and are kept for provenance: the
agent results in `outputs/mynewt/` (claude-opus-4-6) were produced against
these versions. Their images are kept locally as `embedeval:mynewt-<pr>-handmade`.

They were replaced in `docker/instances/` by instances generated from the
reviewed inputs in `triage/mynewt_reviewed/` with
`scripts/generate_instance.py --repo mynewt --pr <pr> --triage triage/mynewt_reviewed/<pr>.json --out docker/instances --tag-suffix=`.
Each generated instance validated 3/3, run one at a time.

What differs:

- Commits. Mynewt merges PRs by rebasing, so these used the rebased commits on
  `master`; the generated ones use the PR branch's own base and head commits
  from the enriched record. That moves the base commit of #3299
  (89ee1a1 -> edce0a6) and #3680 (c2b874b -> 53ed93a); #2809's base is
  unchanged. `fix_commit` is the PR head instead of the last rebased commit.
- #3299 now records its test/fix split explicitly: `test_commit` is the PR's
  test commit, `test_support_paths` holds the SELFTEST-only accessor it adds to
  `kernel/os/src/os_msys.c`, and validation reconstructs the fix from that
  commit (`gold_base_commit`).
- Test patches are identical line for line; problem statements, testcase
  inventories and compatibility flags were carried over unchanged.
- The generated runner and Dockerfile are the current templates (compile-mode
  support, `compatibility_cflags` read from metadata), and metadata gains
  `failure_mode`, `protected_paths`, `change_type` and `generated`.
- #3680's `files_changed_by_fix` also lists the two `encoding/json/hosttest`
  files the PR changed, and its `problem_statement_source`/`scope_note`
  annotations are not carried (the problem statement text is the same).
