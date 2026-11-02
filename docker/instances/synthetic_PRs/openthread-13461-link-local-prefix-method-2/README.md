# openthread-13461-link-local-prefix — Method 2

Method: **Option 2**. Parent: [https://github.com/openthread/openthread/pull/13461](https://github.com/openthread/openthread/pull/13461).

## Original bug mechanism

IPv4-mapped IPv6 text expands to mixed dotted-decimal notation which the legacy parser misinterprets.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `182c0c3f2068746bbed9323719a21905091f75dc` to `a2f000ddf80f66c3d9f9ac268d84e34d675f5bb1`.

## Synthetic bug mechanism and distinctness

Address classification checks a fixed 16-bit prefix instead of the full fe80::/10 link-local range.

The original upstream fix is retained. Address classification checks a fixed 16-bit prefix instead of the full fe80::/10 link-local range. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

fe9a::1 and febf::1 must be link-local; fec0::1 must not. Parsing and mapped-address regressions must still pass.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `tests/scripts/thread-cert/pktverify/addrs.py`.

Tests: `tests/scripts/thread-cert/test_ipv6_addr_regression.py`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 10 | 0 | 0 |
| synthetic | 9 | 1 | 1 |
| restored | 10 | 0 | 0 |

Fail-to-pass tests: `test_link_local_prefix_boundaries`.
Pass-to-pass tests: `test_binary_inputs`, `test_formatting_and_classification`, `test_invalid_inputs`, `test_legacy_string_formats`, `test_mapped_boundary_values`, `test_mapped_compressed_hex`, `test_mapped_dotted_quad`, `test_mapped_expanded_hex`, `test_ordinary_ipv6`.

Exact reason for regression failure: Address classification checks a fixed 16-bit prefix instead of the full fe80::/10 link-local range. fe9a::1 and febf::1 must be link-local; fec0::1 must not. Parsing and mapped-address regressions must still pass. fe9a::1 and febf::1 return False although they are in fe80::/10.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

Replacing an address-library predicate with an overly specific byte-prefix optimization loses prefix-mask semantics.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The original parent image, pinned runtime/compiler, production source, original regression suite, and existing controls are retained. This is a focused host test, not a firmware or radio integration test.

This variation covers adjacent address-classification behavior rather than the original mapped-address parsing path; that is a weaker similarity than the buffer/protocol cases.

## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance openthread-13461-link-local-prefix --build --validate
```

Build command: `python3 -m compileall -q tests/scripts/thread-cert/pktverify tests/scripts/thread-cert/test_ipv6_addr_regression.py`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/tests/scripts/thread-cert/pktverify/addrs.py
+++ b/tests/scripts/thread-cert/pktverify/addrs.py
@@ -121,7 +121,7 @@
         """
         Returns if the Ip6 address is link local.
         """
-        return self._addr.is_link_local
+        return self._addr.packed[:2] == b'\xfe\x80'
 
     @property
     def is_multicast(self) -> bool:
```
