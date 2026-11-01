# openthread-13494-multi-digit-error

Method: **Option 2**. Parent: [https://github.com/openthread/openthread/pull/13494](https://github.com/openthread/openthread/pull/13494).

## Original bug mechanism

Reader delivery can occur just after the asynchronous command wait budget expires.

Original issue/problem description and regression/control lists are preserved in [parent metadata](parent/metadata.json). The exact original production fix is [upstream-fix.diff](build/upstream-fix.diff), from `255021a0b9675061ebd6e6bb3dcec53a68bdae45` to `c9d3215b0f1f685c78b7570b03ceedaa8d583dd9`.

## Synthetic bug mechanism and distinctness

The terminal-response pattern accepts only a single digit in an error code, so a complete Error 12 line is mistaken for unfinished output.

The original upstream fix is retained. The terminal-response pattern accepts only a single digit in an error code, so a complete Error 12 line is mistaken for unfinished output. The original regression behavior remains corrected. This patch is not a reversal of the upstream fix.

## Intended behavior and oracle

A delayed Error 12: LinkMarginLow must return as the terminal response, in order, without ExpectLineTimeoutError.

The oracle was recorded before production injection. A correct test-only image passed before the driver built the buggy image. See [oracle.json](oracle.json) and [pre-injection evidence](evidence/pre-injection-tests.log).

## Files and tests changed

Production: `tools/otci/otci/command_handlers.py`.

Tests: `tests/test_otci_pending_lines.py`.

With the synthetic bug present and the added test patch removed, the entire original suite passes. This was executed and saved in evidence/original-tests-only.log; the new test therefore supplies coverage missing from the original suite.

Build/runner support is recorded in `build/`; tests are extended rather than replaced. See [synthetic-test.diff](synthetic-test.diff). The #13189 runner adds the new named test to its loop; NuttX uses a stricter completion/failure detector for full ostest.

## Validation results

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 12 | 0 | 0 |
| synthetic | 11 | 1 | 1 |
| restored | 12 | 0 | 0 |

Fail-to-pass tests: `test_delayed_multi_digit_error`.
Pass-to-pass tests: `test_continuous_output_timeout`, `test_delayed_diagnostic`, `test_delayed_done`, `test_delayed_error`, `test_delayed_nonmatching_output`, `test_delayed_output_order`, `test_log_filter_and_callback`, `test_missing_completion`, `test_ready_async`, `test_synchronous_command`, `test_upstream_log_filter_assertions`.

Exact reason for regression failure: The terminal-response pattern accepts only a single digit in an error code, so a complete Error 12 line is mistaken for unfinished output. A delayed Error 12: LinkMarginLow must return as the terminal response, in order, without ExpectLineTimeoutError. Error 12 does not match the terminal-response pattern; the runner raises ExpectLineTimeoutError after receiving that complete line.

Test sources and runner hashes match across correct, synthetic, and restored states. Restored production bytes match the correct implementation. Build failures and timeouts are rejected. [Machine-readable results](evidence/results.json) include the pre-injection and original-tests-only checks.

## Realism, similarity, and validity concerns

Dropping a regex quantifier silently restricts valid protocol values while preserving ordinary Done and single-digit errors.

The case exercises the same component and a related behavior, while retaining the parent fix. These are deliberately small, localized semantic bugs and may be relatively easy benchmark tasks. The original parent image, pinned runtime/compiler, production source, original regression suite, and existing controls are retained. This is a focused host test, not a firmware or radio integration test.



## Reproduce

From the portable bundle root:

```sh
python reproduce.py --instance openthread-13494-multi-digit-error --build --validate
```

Build command: `python3 -m compileall -q tools/otci/otci tests/test_otci_pending_lines.py`. Test command: `run_tests` from `/testbed`.
The public [repair.diff](repair.diff) stays outside the image build context. Blinded evaluation should expose only the buggy environment and problem statement.

## Injected production patch

```diff
--- a/tools/otci/otci/command_handlers.py
+++ b/tools/otci/otci/command_handlers.py
@@ -91,7 +91,7 @@
 
 class OtCliCommandRunner(OTCommandHandler):
     __PATTERN_COMMAND_DONE_OR_ERROR = re.compile(
-        r'(Done|Error|Error \d+:.*|.*: command not found)$')  # "Error" for spinel-cli.py
+        r'(Done|Error|Error \d:.*|.*: command not found)$')  # "Error" for spinel-cli.py
 
     __pattern_log_line = re.compile(r'((\[(NONE|CRIT|WARN|NOTE|INFO|DEBG)\])'
                                     r'|(-.*-+: )'  # e.g. -CLI-----:
```
