# Mynewt Phase 1 (redo, NuttX style): status

Branch `mynewt-triage2`. Prompt `docs/mynewt_triage.md`, runner `scripts/triage_mynewt_prs.py`,
partial gold `triage/mynewt_gold.json` (9 must_accept, 4 must_not_propose). Model qwen38-nvfp4 on a
Spark node (Scholar job 479918). Bar: 100% on 2 runs in a row with one unchanged prompt.

| Run | Prompt sha256 | Score | Misses | Repairs |
|---|---|---|---|---|
| v1 (8 cases, before 129/176/938/1594 were added) | 63d8877ea5 | 6/8 | 2809 (modified append_fill in fail_to_pass), 1702 (accepted; test change is cosmetic) | 0 |
| v2 | 471a905921 | 10/12 | 2809 (same), 938 (accepted; socket/timing test) | 1 |
| v3 | 7ce1143cd0 | 11/12 | 3299 (new one-pool control case limit1 in fail_to_pass) | 0 |
