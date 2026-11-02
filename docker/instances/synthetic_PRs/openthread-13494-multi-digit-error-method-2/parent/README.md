# OpenThread #13494

Source: https://github.com/openthread/openthread/pull/13494

The merged upstream change is `c9d3215b0f1f685c78b7570b03ceedaa8d583dd9`.
Its sole parent, `255021a0b9675061ebd6e6bb3dcec53a68bdae45`, is the
benchmark base. GitHub's PR base SHA is
`58427f3571fc6a9e05c1b3e00a60c32dfa9383e4`; the merged commit's parent
isolates the final fix without intervening main-branch changes.
The PR head is `355a80f4729a69b88503952a1742c0008663101e`.
The complete merged diff changes only
`tools/otci/otci/command_handlers.py`, adding bounded handling of reader
output arriving after the virtual-time polling budget is exhausted.

## Tests and scope

The PR adds no upstream tests, so `test_commit` is omitted. `test_patch.diff`
adds only the benchmark-authored `tests/test_otci_pending_lines.py`.

Existing upstream `tools/otci/tests/test_otci.py` contains
`TestOTCI.testCliRealDevice` and `TestOTCI.testCliSimRealTime`. Those require
physical devices or a complete simulator/firmware setup, respectively, and
do not deterministically exercise this scheduling boundary. Neither is
executed or counted as a passing control in this instance.

The seven existing import-time log-filter assertions in upstream
`command_handlers.py` are executed unmodified as existing controls through
`test_upstream_log_filter_assertions`. Six additional focused control methods
cover ready asynchronous responses, synchronous commands, echo handling,
log filtering and callbacks, genuinely missing completion, delayed unrelated
output, and bounded timeout during continuous unrelated output.

Four focused regression methods cover `scan`, `discover`, `ping`, and
`networkdiagnostic get`, including Done, Error, and preservation of response
order. They import the real upstream modules and use the real background
reader thread and pending-lines queue. A scripted `OtCliHandler` transport
holds output behind an event. A runner subclass calls the unchanged upstream
`wait` method and releases the transport only after the final polling step
returns its actual output. Command execution and completion handling remain
entirely upstream code. This forces the reported race without arbitrary
sleeps or modifications to the implementation. The continuous-output control
paces its scripted transport separately to check the bounded timeout.

This is a deterministic command-handler reproducer, not a full virtual-time
simulation or end-to-end firmware test. The tests do not prescribe the
upstream implementation or its exact grace-period duration. They remain
unchanged between base and fixed runs; no failing regression was removed or
weakened.

## Build and runner

The Dockerfile follows the existing OpenThread base-image convention. It
installs Debian's Python 3 and a virtual environment containing
`adb-shell==0.4.4`, which upstream imports when defining its ADB connector.
The suite does not connect to ADB or require other optional transport
dependencies. No submodules, hardware, emulator, or firmware build are needed.

The image shallow-fetches only the base commit, applies only `test_patch.diff`,
and syntax-checks the Python files without running the expected failing
assertions. `build_command` is the same Python compilation check.
`run_tests.sh` runs all 11 unittest methods and uses the repository's exit
statuses: 0 pass, 1 failure, 2 timeout. The existing `tests/` protection applies;
the implementation lives under `tools/`. Per-instance orientation uses the
existing metadata override. No shared EmbedEval files were changed for this
instance; pre-existing working-tree changes were preserved.

## Validation (2026-10-03)

Local validation used CPython 3.13.7 on Windows with the real ADB import
dependency. All eight downloaded OTCI modules were checked against the base
Git blob hashes. The exact merged diff was applied with `git apply`; the
resulting implementation was byte-for-byte identical to the merged Git blob.
Python compilation succeeded at both revisions.

The regression-test SHA256 was unchanged across revisions:
`a84ea3082d23077c115b49db21871f3b2eca18e99d66ea2e99cabe1f555650ec`.

| Test method | Base | Upstream fix |
| --- | --- | --- |
| test_delayed_done | FAIL | PASS |
| test_delayed_error | FAIL | PASS |
| test_delayed_output_order | FAIL | PASS |
| test_delayed_diagnostic | FAIL | PASS |
| test_ready_async | PASS | PASS |
| test_synchronous_command | PASS | PASS |
| test_log_filter_and_callback | PASS | PASS |
| test_missing_completion | PASS | PASS |
| test_delayed_nonmatching_output | PASS | PASS |
| test_continuous_output_timeout | PASS | PASS |
| test_upstream_log_filter_assertions | PASS | PASS |

- **Base regression results:** all four fail with the expected
  `ExpectLineTimeoutError` and exit code 1, rather than import/setup failures.
- **Base control results:** all seven methods pass with exit code 0. The
  upstream-assertion method executes seven existing upstream assertions;
  these are not the full upstream integration tests.
- **Upstream revision results:** all 11 methods pass individually. The full
  suite also passes in 10 consecutive runs, exercising the threaded schedule.
- **Solution-exposure check:** static checks pass for generated agent prompts
  and Docker inputs. The image copies only the regression patch and runner;
  no gold implementation, fix IDs, metadata, or this provenance README is
  copied into the image. The problem statement describes symptoms and
  expected behavior without giving the fix. Container inspection is not
  available without Docker.
- **Metadata validation:** JSON, required instance artifacts, naming,
  registration and discovery in both scripts and harness, build arguments,
  commit IDs, parent relationship, exact changed-file list, disjoint and
  complete test lists, orientation, and protection settings pass. EmbedEval
  supplies no separate metadata-schema validator.
- **Docker validation:** base and instance build dry runs pass. Actual base
  build, instance build, and `validate_instance.py` were attempted but cannot
  start because Docker is absent (`FileNotFoundError`, WinError 2).
  **Docker construction and container-based fail-then-pass validation remain
  unverified**, including the Debian Python runtime.

Bash syntax validation and test-patch application checks pass. Runner and
evaluator selection dry runs pass; the evaluator correctly reports a missing
model patch because no agent run was performed. This is not an upstream patch
grade. `git diff --check` passes.

On a host with Python and a running Docker engine, run from the repository root:

```sh
python scripts/build_bases.py --repo openthread
python scripts/build_instances.py --instance openthread__openthread-13494
python scripts/validate_instance.py openthread__openthread-13494 --verbose
```
