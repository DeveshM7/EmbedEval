# Contiki-NG #817

Source: https://github.com/contiki-ng/contiki-ng/pull/817

The PR was open and unmerged when inspected on 2026-10-09. This candidate
pins its actual head `6844f983629bb8f8b552e78ac1ae1aee2eb87c8c`, rather than
GitHub's synthetic merge revision. Its sole parent,
`423d928e974f78291c4ff46c544ea48a22dbaeb3`, is the appropriate pre-change base.
The API's current target-branch base is not the parent of this historical PR.
Both `fix_commit` and `test_commit` identify the pinned head; tests are
benchmark-authored because the PR adds no tests. If dataset admission requires
merged PRs, this remains a candidate until that requirement is satisfied.

The upstream diff changes only `os/net/app-layer/coap/coap-engine.c`.
The task description specifies observable response behavior without the
upstream code, variable names, or implementation algorithm. The Dockerfile
fetches only the base. The test patch adds four files under the protected
`tests/08-native-runs/embedeval-coap-block2/` directory and changes no
production sources or existing upstream tests.

## Tests and commands

The pinned upstream tree has CoAP/LwM2M integration tests in
`tests/18-coap-lwm2m`, but no focused CoAP Block2 unit suite. Those tests use
native nodes, networking and external client services and do not exercise
this response-size regression directly. This instance uses a host component
suite linking the actual upstream parser, engine, serializer, list library
and random library. Only time, transport, transaction allocation and
observation are fixtures. Discovery is not activated. No receive-path,
parser, serializer or block-selection logic is replaced or extracted.
This validates the component, not the full Contiki native network stack.

Build from the upstream repository root:

```sh
bash tests/08-native-runs/embedeval-coap-block2/build.sh
```

`run_tests` rebuilds and runs all tests. `run_tests regression` and
`run_tests control` select groups. Exit status is 0 for success, 1 for
build/test failure, and 2 for execution timeout. Docker pre-builds without
executing the deliberately failing base tests. No submodules are required.

The regression tests inspect replies after serialization and parsing:

- `small_block`: application-initiated blocks of 16 and 32 bytes, for both
  confirmable and non-confirmable GETs, with no request Block2 option.
- `capped_payload`: a 16-byte block limits a longer response payload.

Five controls preserve existing base behavior: ordinary responses without
Block2, maximum-sized application blocks, offsets above the maximum,
final-block signaling, and explicit client Block2 requests including a
nonzero block number and both continuing and final responses. Every exchange
checks payload contents, token, reply type, response code, handler invocation,
and relevant block number, size, offset and More flag. Confirmable replies
also check the message ID. Existing upstream tests were left untouched.

## Validation (2026-10-09)

Local builds used LLVM-MinGW 20260922 on Windows with the same source list,
C99, O2, include paths and section garbage collection as the packaged build.
The packaged patch was applied to a fresh archived base using `git apply
--check` and `git apply`; the exact upstream production diff was then applied
to that same tree. SHA256 hashes confirm all four test files remained
unchanged across revisions. No test was removed or weakened.

| Check | Result |
| --- | --- |
| Base regression | 2/2 FAIL; group exits 1 |
| Base controls | 5/5 PASS; group exits 0 |
| Pinned PR head | 7/7 PASS; both groups exit 0 |
| Metadata | PASS: conventional fields/types, test IDs, parent revision, changed file, discovery, project configuration and build arguments |
| Structure | PASS: four required artifacts, test-only patch, LF endings, patch applicability, shell syntax and build dry runs |
| Task description | PASS: behavioral requirements, no upstream implementation |
| Docker | **UNVERIFIED**: Docker executable unavailable |

The repository-native base builder, instance builder and validator were
attempted and each stopped at Docker launch with `FileNotFoundError
[WinError 2]`. Dry runs establish selection/configuration only; neither a
Linux image build nor container fail-then-pass validation has been completed.

Complete Docker validation on a host with Docker:

```sh
python scripts/build_bases.py --repo contiki-ng
python scripts/build_instances.py --instance contiki-ng__contiki-ng-817
python scripts/validate_instance.py contiki-ng__contiki-ng-817 --verbose
```

Only this new instance was added. Existing project registration, base image
configuration, the #3268 instance and all pre-existing working-tree changes
were preserved. Temporary preparation, audit scripts and local results are
under ignored `.venv/pr817/`.
