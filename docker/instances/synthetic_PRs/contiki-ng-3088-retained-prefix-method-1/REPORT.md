# contiki-ng-3088-retained-prefix

Synthetic instance derived from [parent PR](https://github.com/contiki-ng/contiki-ng/pull/3088). Method: **Option 1**.

## Problem statement

TCP input retention preserves the wrong bytes: a callback retaining a suffix receives an old prefix on the next delivery. Preserve the requested trailing bytes in order across deliveries.

## Mechanism and provenance

Original parent issue: TCP socket input callbacks can request that an unconsumed suffix remain buffered, but those bytes are lost or overwritten when more data arrives. Retained bytes must be presented in order together with subsequent data, including when a single incoming segment exceeds the input buffer size. A newly registered socket must start with no retained data. Requests to retain more bytes than were delivered must stay bounded, and receiving more data while the buffer is entirely retained must return without overwriting it or looping indefinitely. Preserve ordinary reception, binary payloads, callbacks that consume all data, sockets without input callbacks, and send-queue behavior.

Synthetic mechanism: Compaction copies from the buffer start rather than the retained suffix. Retention lengths, bounds checks, and registration initialization remain correct.

Correct source commit: `ca16edcecbb6196685ce66794811446fac565228`. Original base: `cf61c0dfa134665edff6315e13b9345c0b713d2f`.
Production source: `os/net/ipv6/tcp-socket.c`. The mutation is relative to the correct upstream implementation, not a reversal of the parent fix.

```diff
--- a/os/net/ipv6/tcp-socket.c
+++ b/os/net/ipv6/tcp-socket.c
@@ -135,7 +135,7 @@
               " than in buffer (%d)\n", bytesleft, inputlen);
         bytesleft = inputlen;
       }
-      memmove(s->input_data_ptr, s->input_data_ptr + inputlen - bytesleft, bytesleft);
+      memmove(s->input_data_ptr, s->input_data_ptr, bytesleft);
     }
     dataptr += copylen;
     len -= copylen;
```

## Validation

| State | Pass | Fail | Exit |
|---|---:|---:|---:|
| correct | 18 | 0 | 0 |
| synthetic | 15 | 3 | 1 |
| restored | 18 | 0 | 0 |

Expected failures: `retain_across_events`, `retain_within_large_event`, `suffix_compaction`.
All other listed tests remain passing. Source hashes verify restoration; hashes of every test file and the runner remain identical across all three states.
See [machine-readable evidence](evidence/results.json) and per-state build and test logs in [evidence/](evidence/).

Native component tests of real upstream production source; not full RTOS, radio, or hardware validation.

## Reproduce

From the bundle root:

```sh
python reproduce.py --instance contiki-ng-3088-retained-prefix --build --validate
```

`repair.diff` is the gold repair and stays outside the Docker build context. This public review bundle exposes solutions; blinded evaluation should provide only the intended buggy environment and problem statement.
