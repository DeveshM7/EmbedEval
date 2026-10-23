# Hand-written NuttX instances (archived 2026-10-11)

These six instances were written by hand and are kept for provenance: the
agent results in `outputs/nuttx/` (claude-opus-4-6, gpt-5.4, gemini-2.5-pro,
DeepSeek-V3, Qwen-3-coder) were produced against these versions.

They were replaced in `docker/instances/` by instances generated from the
reviewed inputs in `triage/nuttx_reviewed/` with
`scripts/generate_nuttx_instance.py --out docker/instances --tag-suffix=`.
The generated versions differ: they use the PR branch base..tip commits
instead of merge commits, include only `testing/ostest/` files in the test
patch, keep assertions on (`CONFIG_NDEBUG=n`), and have no `make || true`
prebuild. Problem statements were carried over; #12802's API names were
corrected (`nxevent_*`, not `nx_event_*`).

Known defects of these archived versions (#12802 had no run_tests install
and no `CONFIG_SCHED_EVENTS`; #8885's fix file list was incomplete) were
repaired here before archiving; see git history.
