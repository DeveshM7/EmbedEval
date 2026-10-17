# NuttX Phase 1 tuning status

Generation 0 stopped at the pass bar (updated 2026-10-17 03:13).

- Bar: one unchanged individual with 2 repeats at 13/13 on the normal gold; blind runs
  (9 cases with a must_find) are tracked only.
- Best count: 2 perfect normal repeats of one individual (need 2).
- Winner: `68660ee811`, unchanged normal repeats 2 and 3 both 13/13;
  repeat 1 was 12/13. Blind repeats 1 and 2 were 8/9 each.
- Individuals scored so far: 30; scorecards at stop: 121 of 180 queued tasks.
- Fitness: mean normal score over repeats; ties by worst repeat, then mean blind.

GA pool jobs at the stopping point (477263 was canceled pending; the 477254
worker step was stopped, and its pool remains temporarily for an ARM64 image build):

```
477251 spark-batch 11 nodes, ends N/A
477254 spark-interactive 64 nodes, ends 2026-10-17T05:50:48
```

## Top 5

| sha | rules | knobs changed | normal per repeat | blind per repeat | mean | worst |
|---|---|---|---|---|---|---|
| `7e86ad0ca2` | config_branch | - | [13] | [8] | 13.00 | 13 |
| `c2e87ae44c` | final_review | - | [13] | [8] | 13.00 | 13 |
| `35c551478a` | (base prompt) | {'temperature': 0.6} | [13] | [7] | 13.00 | 13 |
| `68660ee811` | config_input, config_branch, followup_include, followup_new_code, plumbing_low, exposed_compile, final_review, answer_when_done | - | [12, 13, 13] | [8, 8] | 12.67 | 12 |
| `244dc3057f` | config_input, config_branch, followup_include, followup_new_code, plumbing_low, exposed_compile | {'temperature': 0.6} | [13, 12] | [9, 8] | 12.50 | 12 |

## Next experiments

Finish the ARM64 QEMU base-image build, release the remaining GA pool, and run
Phase 2 on all 189 apps PRs with `68660ee811`. No further GA experiments are
needed under the two-repeat bar.
