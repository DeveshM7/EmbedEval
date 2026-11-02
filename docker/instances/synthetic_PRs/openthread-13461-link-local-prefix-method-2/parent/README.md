# OpenThread #13461

Source: https://github.com/openthread/openthread/pull/13461

The merged fix is `a2f000ddf80f66c3d9f9ac268d84e34d675f5bb1`.
Its sole parent, `182c0c3f2068746bbed9323719a21905091f75dc`, is the
benchmark base and also GitHub's PR base SHA. The PR head is
`d10d9dee5afed0c30cd3145f3ddace2f99123f8b`.
The merged diff changes only `tests/scripts/thread-cert/pktverify/addrs.py`.

The PR adds no tests. Therefore `test_commit` is omitted:
`test_patch.diff` contains benchmark-authored regression tests. They import
the real upstream `Ipv6Addr` and `Bytes` modules without mocks or substituted
implementations. This is a deterministic parser-level reproducer for the
packet-verification failure described in the PR, rather than the full
`1_4_DNS_TC_5` certification test.

## Runtime

Python 3.13.7 is pinned using `python:3.13.7-slim-bookworm`. The instance
copies that runtime into the existing OpenThread base and installs its
runtime libraries. On this version, expanding an IPv4-mapped IPv6 address
produces a dotted-quad suffix, which the base parser rejects. Using an older
runtime can hide the bug. The test runner checks both the Python version and
the relevant standard-library behavior, so an incompatible runtime fails
clearly instead of giving a free pass. No pip packages, submodules, firmware
build, packet captures, or radio hardware are required.

`build_command` compiles the Python modules without running the failing
assertions. `run_tests` runs all nine unittest methods under a timeout and
returns the repository's standard statuses: 0 pass, 1 failure, 2 timeout.

## Harness integration

The implementation happens to live under `tests/`. Protecting all of that
directory would prohibit and discard the correct fix. This instance therefore
overrides `protected_paths` to protect the regression test and its supporting
upstream modules while permitting changes to `pktverify/addrs.py`.
`harness/run.py` now uses the existing `projects.setting` mechanism for
protection in both the prompt and patch capture, and for the instance's
Python-specific orientation. Instances without overrides retain their project
defaults, including the previous OpenThread frame-buffer instance.

The image checks out only the base commit and applies only the test patch.
It does not copy metadata, this README, or the upstream fix into the image.
The problem statement describes symptoms and expected behavior without
describing the upstream implementation.

## Validation

Local verification used portable CPython 3.13.7 on Windows and the exact
upstream source at the two commits. Compilation succeeded before and after
applying the gold diff restricted to `files_changed_by_fix`. The regression
test was unchanged between runs.

| Test method | Base | Upstream fix |
| --- | --- | --- |
| test_mapped_expanded_hex | FAIL | PASS |
| test_mapped_compressed_hex | FAIL | PASS |
| test_mapped_dotted_quad | FAIL | PASS |
| test_mapped_boundary_values | FAIL | PASS |
| test_ordinary_ipv6 | PASS | PASS |
| test_legacy_string_formats | PASS | PASS |
| test_binary_inputs | PASS | PASS |
| test_formatting_and_classification | PASS | PASS |
| test_invalid_inputs | PASS | PASS |

The tests check literal expected bytes, mapped boundary values, legacy
input formats, binary inputs, formatting round trips, address classification,
and rejection of malformed inputs. The multicast classification control
preserves upstream behavior on the pinned runtime.

The test patch applied cleanly to the base. EmbedEval base and instance build
dry runs passed, Bash syntax validation passed, and harness prompt/protection
override checks passed. Actual image builds and `validate_instance.py` were
attempted but could not start because Docker is not installed.
**Docker image construction and container-based fail-then-pass validation
remain unverified.** On a host with Python and a running Docker engine, run
from the EmbedEval repository root:

```sh
python scripts/build_bases.py --repo openthread
python scripts/build_instances.py --instance openthread__openthread-13461
python scripts/validate_instance.py openthread__openthread-13461 --verbose
```

### Validation audit (2026-10-03)

All available checks were repeated, using CPython 3.13.7 and exact Git blobs
for the base and merged upstream implementation. The same regression test
was used for both revisions (SHA256
`cc090ba017599b3d20e5be4126f034b2b9d24748a097cedc191a24103855b76b`).

| Category | Result |
| --- | --- |
| Base revision regression results | All 4 regression methods fail as expected with exit code 1; mapped address construction raises ValueError. |
| Base revision control results | All 5 control methods pass with exit code 0. |
| Upstream revision results | All 9 methods pass with exit code 0. Python compilation succeeds on both revisions. |
| Solution-exposure check | Static checks pass: generated system/instance/task prompts and copied build inputs contain no gold implementation or fix commit IDs. Docker copies only the test patch and runner; metadata and provenance README stay outside the image. In-container inspection is unavailable. |
| Metadata validation | Checks pass for JSON parsing, required fields, instance/image naming, project registration, build/harness discovery, resolved build arguments, full commit IDs, sole-parent relationship, exact fix paths, all 9 test names, disjoint regression/control lists, and protection of the regression test while permitting the fix. No standalone schema validator is provided by EmbedEval. |
| Docker validation | Base and instance build dry runs pass. Actual base build, instance build, and fail-then-pass validator cannot start: Docker executable missing (FileNotFoundError, WinError 2). Container validation is NOT verified. |

Runner and evaluator selection dry runs also succeed. The evaluator reports
the model patch as missing because no agent run was requested or performed;
this dry run does not grade the upstream solution. Bash syntax validation,
test-patch application checks, and `git diff --check` pass. The earlier
gold-diff validation and this exact-upstream-blob replay give the same results.
