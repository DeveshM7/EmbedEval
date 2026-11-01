# zephyr-65697-destructor — Method 2

This is one synthetic bug derived from [parent PR #65697](https://github.com/zephyrproject-rtos/zephyr/pull/65697);
it is not a new upstream PR. Method: **Option 2**.

## Problem statement

POSIX thread-specific data registered with a non-NULL pthread key destructor is not cleaned up when the owning thread exits. A worker can successfully associate a non-NULL value and terminate, but the registered callback is never invoked. The destructor must run once for this worker's non-NULL value, receiving that same pointer. Preserve existing key allocation, deletion, and thread-specific association behavior.

## Exact injected bug

The original upstream fix is applied first. This patch introduces the distinct
synthetic bug on top of the correct implementation:

```diff
--- a/lib/posix/key.c
+++ b/lib/posix/key.c
@@ -109,7 +109,6 @@
 
 	sys_slist_init(&(new_key->key_data_l));
 
-	new_key->destructor = destructor;
 
 	return 0;
 }
```

Production source: `lib/posix/key.c`. One new destructor regression was added; all existing tests are preserved.
The expected failing tests are:

- `test_key_thread_exit_calls_destructor`

## Validated results

| State | Passing tests | Failing tests |
|---|---:|---:|
| Correct implementation | 52 | 0 |
| Synthetic bug | 51 | 1 |
| Restored implementation | 52 | 0 |

All relevant controls passed. A fresh packaged image reproduced the expected
failures, and applying only `repair.diff` made the entire suite pass.
See [the report](REPORT.md) and [saved evidence](evidence/) for details and limits.
These are historical results; newly executed results are stored separately.

## Reproduce from the exported repository root

```sh
python reproduce.py --instance zephyr-65697-destructor --build --validate
```

Run the buggy image directly after building it (expected exit 1):

```sh
docker run --rm embedeval:zephyr-65697-synthetic-destructor bash -c "cd /testbed && run_tests 600"
```

The repair patch is deliberately outside `build/`, so it is not copied into the
instance image. This public review repository exposes both the bug and repair;
an agent evaluation must provide only the intended instance environment.
