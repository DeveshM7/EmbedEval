# contiki-ng-3214-shared-transmit-cell

Method: **Option 2**. Parent: [https://github.com/contiki-ng/contiki-ng/pull/3214](https://github.com/contiki-ng/contiki-ng/pull/3214).

## Original bug mechanism

Time-source changes remove still-needed neighbor transmit links and discard queued packets.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `7f844118d0235db405cae54f69cc1d4e7f0bd788` to `d806f5dc295615789d1b42e8ddd4e3571f5789a0`.

## Synthetic bug mechanism and distinctness

Neighbor transmit cells retain their links but omit LINK_OPTION_SHARED, disabling the shared-cell contention policy.

The original upstream fix is retained. Neighbor transmit cells retain their links but omit LINK_OPTION_SHARED, disabling the shared-cell contention policy. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

A newly added neighbor must have a transmit link marked both TX and SHARED. Existing time-source, queue-lifetime, and receive-cell tests must remain passing.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `os/services/orchestra/orchestra-rule-unicast-per-neighbor-rpl-ns.c`.

Tests: `tests/08-native-runs/embedeval-orchestra/main.c`, `tests/08-native-runs/embedeval-orchestra/run.py`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 19 | 0 | 0 |
| synthetic | 18 | 1 | 1 |
| restored | 19 | 0 | 0 |

Fail-to-pass tests: `neighbor_transmit_cell_shared`.
Pass-to-pass tests: `broadcast_not_selected`, `data_packet_select`, `initial_receive_cell`, `loss_preserves_neighbor_link`, `neighbor_added_has_tx`, `neighbor_removed_cleanup`, `non_neighbor_not_scheduled`, `nondata_not_selected`, `queued_frames_not_discarded`, `switch_preserves_neighbor_link`, `unchanged_source_preserved`, `unrelated_neighbor_preserved`, `upstream_test_cdll`, `upstream_test_csll`, `upstream_test_dll`, `upstream_test_list`, `upstream_test_queue`, `upstream_test_stack`.

Exact reason for regression failure: Neighbor transmit cells retain their links but omit LINK_OPTION_SHARED, disabling the shared-cell contention policy. A newly added neighbor must have a transmit link marked both TX and SHARED. Existing time-source, queue-lifetime, and receive-cell tests must remain passing. The cell exists and has TX, but the TX|SHARED mask assertion fails.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

Omitting a link-option flag when creating scheduler cells is a localized configuration mistake; this differs from link deletion.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The original parent image, component build, fixtures, original regression suite, and all existing controls are retained. Existing upstream data-structure controls are run where provided by the parent. This is a host component test, not a Cooja or physical-radio test.

The regression checks shared-cell configuration rather than actual contention or radio delivery.

## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance contiki-ng-3214-shared-transmit-cell --build --validate
```

Build command: `bash tests/08-native-runs/embedeval-orchestra/build.sh`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/os/services/orchestra/orchestra-rule-unicast-per-neighbor-rpl-ns.c
+++ b/os/services/orchestra/orchestra-rule-unicast-per-neighbor-rpl-ns.c
@@ -80,7 +80,7 @@
      * at that cell. The channel offset here does not matter:
      * select_packet() always sets the right channel offset per packet. */
     tsch_schedule_add_link(sf_unicast,
-        LINK_OPTION_SHARED | LINK_OPTION_TX,
+        LINK_OPTION_TX,
         LINK_TYPE_NORMAL, &tsch_broadcast_address,
         timeslot, 0, 0);
   }
```
