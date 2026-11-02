> Historical EmbedEval validation report. Its commands refer to the original workspace; use this folder's README and the exported reproduce.py for portable commands.

# OpenThread #13461: mapped-family synthetic instance

Method: **Option 1**. Exactly **one synthetic production bug** was created.
Tests changed: **none**. Status: three-state validation and fresh packaged-image
verification passed.

## Parent and original mechanism

Parent: `openthread__openthread-13461`.
PR: https://github.com/openthread/openthread/pull/13461.
Pinned base: `182c0c3f2068746bbed9323719a21905091f75dc`.
Pinned upstream production fix: `a2f000ddf80f66c3d9f9ac268d84e34d675f5bb1`.

Expanded IPv4-mapped IPv6 text includes a dotted-quad suffix that the legacy byte parser cannot decode on Python 3.13.

The parent problem statement, regression tests, controls, relevant source, and
exact upstream production diff were inspected. Docker reproduced the original
expected regressions while all parent controls passed, then the exact fixed
upstream source passed the entire existing suite. Correct-source equality to
the pinned fixed Git blob was checked before injection.

## Synthetic mechanism, distinction, and pre-injection oracle

Normalizing a mapped IPv6 address to the underlying IPv4Address drops the 96-bit IPv6 mapping prefix and produces four bytes instead of sixteen.

This retains the original fix and changes a different semantic behavior rather
than reverting the original patch. The mechanism and oracle were communicated
before injection and recorded in `oracle.json`.

Oracle: Every accepted mapped IPv6 string must yield the literal expected 16-byte IPv6 representation; ordinary IPv6, legacy forms, binary inputs, and invalid-input handling retain their behavior.

Expected regression reason: The 16-byte invariant rejects a four-byte IPv4 payload with ValueError. This is a wrong-family binary representation, not the original dotted-text parsing failure.

Synthetic problem statement: IPv4-mapped IPv6 strings are rejected even though equivalent 16-byte inputs work. Preserve their complete IPv6 representation across expanded, compressed, and dotted-quad forms. Ordinary IPv6, legacy strings, binary inputs, classification, and malformed-input rejection must continue working.

## Files and tests

Only `tests/scripts/thread-cert/pktverify/addrs.py` changes in the synthetic codebase. Exact injection:

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

`repair.diff` reverses only the synthetic change. The fixed source snapshots,
upstream production diff, test logs, build logs, and per-test JSON results are
alongside this report. `tests/scripts/thread-cert/test_ipv6_addr_regression.py` and the original `run_tests` runner
are unchanged in every phase; their combined SHA256 is:

`3b48fd7511664b4b11446296eb23cf0c04242dc07d094ac02ddf8dafb1a35588`

No new test or replacement oracle was needed. The existing parent test patches
are benchmark-authored (the upstream PRs added no tests); they remain unchanged.
The original host instance files, metadata, shared Dockerfiles, harness, and
registry were not edited. Separate LF-normalized copies of original Windows
build contexts were used without changing logical file content.

## Results

| Phase | Build exit | Passing methods/cases | Failing methods/cases | Runner exit |
|---|---:|---:|---:|---:|
| original | 0 | 5 | 4 | 1 |
| correct | 0 | 9 | 0 | 0 |
| synthetic | 0 | 5 | 4 | 1 |
| restored | 0 | 9 | 0 | 0 |
| packaged | 0 | 5 | 4 | 1 |
| packaged-repaired | 0 | 9 | 0 | 0 |

Failing tests with the synthetic bug:

- `test_mapped_expanded_hex`
- `test_mapped_compressed_hex`
- `test_mapped_dotted_quad`
- `test_mapped_boundary_values`

All other existing tests pass with the synthetic bug, including all original
parent controls. Restoring the correct implementation is byte-identical to the
correct baseline and returns every test to PASS. The fresh packaged image
reproduces exactly the same failures; applying only `repair.diff` makes it pass.
No timeout or compilation failure was accepted as the intended regression.

Observed failure: `ValueError: b'\xc0\xa8\xd9d'` for 192.168.217.100, plus four-byte boundary payloads. unittest reports ERROR subcases because valid addresses unexpectedly raise an exception; these are semantic regression failures, not setup/import failures. There are four failing methods and eight error subcases.


## Commands and artifacts

Run from the EmbedEval root. Build all three parent images once with:

```powershell
docker build -f docker/bases/openthread.Dockerfile -t embedeval-openthread-base:latest docker/bases
& .venv/pr13461/python/python.exe outputs/synthetic/openthread-three/prepare_parents.py
```

Replay this case, build its synthetic image, and verify the packaged repair:

```powershell
& .venv/pr13461/python/python.exe outputs/synthetic/openthread-three/validate.py --only 13461
```

Any working Python 3.10+ may replace the explicit host interpreter; validation
requires Docker, Git/network access, and the existing container dependencies,
not an API key or agent libraries. The instance image retains its parent's
runtime, compiler, build command, test source, and runner.

Rebuild the already generated synthetic context directly:

```powershell
docker build -t embedeval:openthread-13461-synthetic-mapped-family outputs/synthetic/openthread-three/13461/instance
```

Run the buggy instance (expected exit 1):

```powershell
docker run --rm embedeval:openthread-13461-synthetic-mapped-family bash -c "cd /testbed && run_tests"
```

The validator also runs the metadata build command before every suite. Exact
commands for each build appear in the parent metadata and `instance/Dockerfile`.
`results.json` records immutable parent/synthetic image IDs and all outcomes.
No validation containers are left running.

## Realism and validity limits

A network-address normalization refactor can incorrectly collapse address families, although a packet verifier must preserve the complete on-wire IPv6 identity.

These are small semantic changes with short repairs, likely low difficulty.
They are not syntax errors or test-specific special cases. Similarity is in
the same real subsystem and exercised workflow; the violated invariant differs
from the original bug mechanism. Only one bug is injected into each image.

Nine unittest methods run against the real upstream Ipv6Addr/Bytes modules on the pinned Python 3.13.7 runtime. No parser implementation is mocked.

The preserved tests establish these focused behaviors, not complete subsystem
correctness, security, performance, or hardware compatibility. Parent dependency
installation remains as unpinned as its original Dockerfiles. Stored image IDs
identify the actual locally validated artifacts.

These are standalone, explicitly synthetic instances. Existing EmbedEval IDs
encode real numeric PRs, so no fake PR ID or harness registry change was made.
The repair patch, reports, and validation scripts stay outside the Docker build
context and are not installed in the image. Preparation patches are removed
from the running filesystem after application; this is not a forensic claim
about inaccessible underlying Docker layers.
