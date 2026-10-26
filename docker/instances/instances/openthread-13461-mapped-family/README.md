# openthread-13461-mapped-family

This is one synthetic bug derived from [parent PR #13461](https://github.com/openthread/openthread/pull/13461);
it is not a new upstream PR. Method: **Option 1**.

## Problem statement

IPv4-mapped IPv6 strings are rejected even though equivalent 16-byte inputs work. Preserve their complete IPv6 representation across expanded, compressed, and dotted-quad forms. Ordinary IPv6, legacy strings, binary inputs, classification, and malformed-input rejection must continue working.

## Exact injected bug

The original upstream fix is applied first. This patch introduces the distinct
synthetic bug on top of the correct implementation:

```diff
--- a/tests/scripts/thread-cert/pktverify/addrs.py
+++ b/tests/scripts/thread-cert/pktverify/addrs.py
@@ -74,7 +74,8 @@
     def __init__(self, addr: Union[str, bytearray, 'Bytes']):
         if isinstance(addr, str):
             try:
-                addr = ipaddress.IPv6Address(addr).packed
+                parsed = ipaddress.IPv6Address(addr)
+                addr = (parsed.ipv4_mapped or parsed).packed
             except ipaddress.AddressValueError:
                 pass
```

Production source: `tests/scripts/thread-cert/pktverify/addrs.py`. Existing regression tests and controls are unchanged.
The expected failing tests are:

- `test_mapped_expanded_hex`
- `test_mapped_compressed_hex`
- `test_mapped_dotted_quad`
- `test_mapped_boundary_values`

## Validated results

| State | Passing tests | Failing tests |
|---|---:|---:|
| Correct implementation | 9 | 0 |
| Synthetic bug | 5 | 4 |
| Restored implementation | 9 | 0 |

All relevant controls passed. A fresh packaged image reproduced the expected
failures, and applying only `repair.diff` made the entire suite pass.
See [the report](REPORT.md) and [saved evidence](evidence/) for details and limits.
These are historical results; newly executed results are stored separately.

## Reproduce from the exported repository root

```sh
python reproduce.py --instance openthread-13461-mapped-family --build --validate
```

Run the buggy image directly after building it (expected exit 1):

```sh
docker run --rm embedeval:openthread-13461-synthetic-mapped-family bash -c "cd /testbed && run_tests"
```

The repair patch is deliberately outside `build/`, so it is not copied into the
instance image. This public review repository exposes both the bug and repair;
an agent evaluation must provide only the intended instance environment.
