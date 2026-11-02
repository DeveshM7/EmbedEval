# contiki-ng-817-wide-block-offset — Method 2

Method: **Option 2**. Parent: [https://github.com/contiki-ng/contiki-ng/pull/817](https://github.com/contiki-ng/contiki-ng/pull/817).

## Original bug mechanism

Resource-initiated blockwise responses advertise the maximum size instead of the smaller resource-selected size.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `423d928e974f78291c4ff46c544ea48a22dbaeb3` to `6844f983629bb8f8b552e78ac1ae1aee2eb87c8c`.

## Synthetic bug mechanism and distinctness

A 32-bit continuation offset is narrowed before its range check. Offset 65552 wraps to 16 and incorrectly selects a tiny block size.

The original upstream fix is retained. A 32-bit continuation offset is narrowed before its range check. Offset 65552 wraps to 16 and incorrectly selects a tiny block size. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

Resource offset 65552 exceeds the maximum block size and must keep the maximum-size advertisement; its low 16 bits must not determine negotiation.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `os/net/app-layer/coap/coap-engine.c`.

Tests: `tests/08-native-runs/embedeval-coap-block2/main.c`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 8 | 0 | 0 |
| synthetic | 7 | 1 | 1 |
| restored | 8 | 0 | 0 |

Fail-to-pass tests: `wide_resource_offset`.
Pass-to-pass tests: `capped_payload`, `explicit_block`, `final_block`, `large_offset`, `maximum_block`, `ordinary_response`, `small_block`.

Exact reason for regression failure: A 32-bit continuation offset is narrowed before its range check. Offset 65552 wraps to 16 and incorrectly selects a tiny block size. Resource offset 65552 exceeds the maximum block size and must keep the maximum-size advertisement; its low 16 bits must not determine negotiation. Offset 65552 becomes 16 in the range check, so the serialized response advertises 16 instead of COAP_MAX_BLOCK_SIZE.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

Comparing after narrowing to the destination field type is a realistic integer-range mistake.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The original parent image, component build, fixtures, original regression suite, and all existing controls are retained. Existing upstream data-structure controls are run where provided by the parent. This is a host component test, not a Cooja or physical-radio test.



## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance contiki-ng-817-wide-block-offset --build --validate
```

Build command: `bash tests/08-native-runs/embedeval-coap-block2/build.sh`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/os/net/app-layer/coap/coap-engine.c
+++ b/os/net/app-layer/coap/coap-engine.c
@@ -267,7 +267,7 @@
                 /* Resource requested Block2 transfer */
               } else if(new_offset != 0) {
                 uint16_t block_size = COAP_MAX_BLOCK_SIZE;
-                if (new_offset > 0 && new_offset < COAP_MAX_BLOCK_SIZE) {
+                if (new_offset > 0 && (uint16_t)new_offset < COAP_MAX_BLOCK_SIZE) {
                     block_size = (uint16_t) new_offset;
                 }
                 LOG_DBG("Blockwise: no block option for blockwise resource, using block size %u\n",
```
