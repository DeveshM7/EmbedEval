# openthread-13494-late-order

This is one synthetic bug derived from [parent PR #13494](https://github.com/openthread/openthread/pull/13494);
it is not a new upstream PR. Method: **Option 1**.

## Problem statement

Late ping and networkdiagnostic responses return completion before their response data, with data lines in the wrong order. Preserve transport arrival order through command completion, including responses arriving after the virtual-time polling budget. Retain bounded timeouts and existing synchronous, ready-response, callback, and error behavior.

## Exact injected bug

The original upstream fix is applied first. This patch introduces the distinct
synthetic bug on top of the correct implementation:

```diff
--- a/tools/otci/otci/command_handlers.py
+++ b/tools/otci/otci/command_handlers.py
@@ -225,7 +225,7 @@
                     except queue.Empty:
                         break
 
-                    output.append(line)
+                    output.insert(0, line)
 
                     if match_line(line, expect_line):
                         done = True
```

Production source: `tools/otci/otci/command_handlers.py`. Existing regression tests and controls are unchanged.
The expected failing tests are:

- `test_delayed_output_order`
- `test_delayed_diagnostic`

## Validated results

| State | Passing tests | Failing tests |
|---|---:|---:|
| Correct implementation | 11 | 0 |
| Synthetic bug | 9 | 2 |
| Restored implementation | 11 | 0 |

All relevant controls passed. A fresh packaged image reproduced the expected
failures, and applying only `repair.diff` made the entire suite pass.
See [the report](REPORT.md) and [saved evidence](evidence/) for details and limits.
These are historical results; newly executed results are stored separately.

## Reproduce from the exported repository root

```sh
python reproduce.py --instance openthread-13494-late-order --build --validate
```

Run the buggy image directly after building it (expected exit 1):

```sh
docker run --rm embedeval:openthread-13494-synthetic-late-order bash -c "cd /testbed && run_tests"
```

The repair patch is deliberately outside `build/`, so it is not copied into the
instance image. This public review repository exposes both the bug and repair;
an agent evaluation must provide only the intended instance environment.
