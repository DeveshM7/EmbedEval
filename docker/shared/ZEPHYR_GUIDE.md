# Zephyr RTOS — Quick Reference for Bug Fixing

## Repository Layout

```
/testbed/
├── include/zephyr/         # Public headers (APIs that subsystems expose)
│   ├── kernel.h            # Kernel primitives (threads, semaphores, etc.)
│   ├── logging/            # Logging subsystem API
│   │   ├── log.h           # LOG_ERR, LOG_WRN, LOG_INF macros
│   │   ├── log_backend.h   # Backend API (init, put, process, etc.)
│   │   └── log_core.h      # Core logging internals
│   ├── net/                # Networking subsystem API
│   │   ├── dhcpv4.h        # DHCPv4 client API
│   │   ├── net_pkt.h       # Network packet API
│   │   └── ...
│   ├── rtio/               # Real-Time I/O subsystem
│   │   └── rtio.h          # RTIO API (submission/completion queues)
│   ├── posix/              # POSIX compatibility layer
│   └── ...
├── subsys/                 # Subsystem implementations
│   ├── logging/            # Logging core + backends
│   │   ├── log_core.c      # Main logging engine
│   │   └── backends/       # UART, console, net, etc.
│   ├── net/                # Networking stack
│   │   └── lib/            # Protocol implementations
│   │       ├── dhcpv4/     # DHCPv4 client
│   │       └── ...
│   └── ...
├── lib/                    # Library code
│   └── posix/              # POSIX API implementations
│       ├── key.c           # pthread_key_* functions
│       ├── mutex.c         # pthread_mutex_* functions
│       └── ...
├── kernel/                 # Kernel implementation
├── tests/                  # Test suite (DO NOT MODIFY)
│   ├── subsys/             # Tests for subsystems
│   ├── net/                # Tests for networking
│   ├── posix/              # Tests for POSIX layer
│   └── ...
├── boards/                 # Board definitions
├── scripts/                # Build scripts, west commands
└── CMakeLists.txt          # Top-level CMake
```

## Key Concepts

### West Build System

West is Zephyr's meta-tool. All builds go through it:

```bash
# Build for a target board
west build -b qemu_x86 tests/path/to/test

# Incremental rebuild (after editing source)
west build

# Clean rebuild
rm -rf build && west build -b qemu_x86 tests/path/to/test

# Run (for QEMU targets — use run_tests instead)
west build -t run
```

**Important**: Always use `run_tests` instead of `west build -t run` directly.
QEMU never exits cleanly, so `west build -t run` will hang.

### Kconfig

Zephyr uses Kconfig for compile-time configuration:
- `prj.conf` in the test directory sets options
- `CONFIG_*` symbols control what's compiled
- Options are defined in `Kconfig` files throughout the tree
- You can pass extra configs: `west build -b qemu_x86 path -- -DCONFIG_FOO=y`

### Zephyr Test Framework (ztest)

Tests use the ztest framework:

```c
#include <zephyr/ztest.h>

ZTEST(suite_name, test_function_name)
{
    zassert_equal(a, b, "message");
    zassert_true(condition, "message");
    zassert_ok(ret, "message");
}

ZTEST_SUITE(suite_name, NULL, NULL, NULL, NULL, NULL);
```

Test output format:
```
START - test_function_name
 PASS - test_function_name in 0.001 seconds
===================================================================
PROJECT EXECUTION SUCCESSFUL
```

Or on failure:
```
START - test_function_name
    Assertion failed at path/to/file.c:42: (condition)
 FAIL - test_function_name in 0.001 seconds
===================================================================
PROJECT EXECUTION FAILED
```

### testcase.yaml

Each test directory has a `testcase.yaml` that defines test scenarios:

```yaml
tests:
  test.scenario.name:
    tags: subsystem_name
    extra_configs:
      - CONFIG_OPTION=y
    integration_platforms:
      - native_sim
```

## Common Patterns

### Atomic Operations

```c
#include <zephyr/sys/atomic.h>

atomic_t counter;
atomic_inc(&counter);           // increment
atomic_get(&counter);           // read
atomic_set(&counter, value);    // write
atomic_cas(&counter, old, new); // compare-and-swap
```

**Warning**: `atomic_t` may be signed on some platforms. Use casts through
`uintptr_t` if you need well-defined unsigned wrapping behavior.

### Bitarray

```c
#include <zephyr/sys/bitarray.h>

sys_bitarray_alloc(bitarray, num_bits, &offset);
sys_bitarray_free(bitarray, num_bits, offset);
```

### Network Packets

```c
#include <zephyr/net/net_pkt.h>

net_pkt_read_u8(pkt, &byte);     // read 1 byte
net_pkt_read_be32(pkt, &val);    // read 4 bytes big-endian
net_pkt_skip(pkt, count);        // skip N bytes
net_pkt_write(pkt, data, len);   // write data
```

### Logging

```c
#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(module_name, LOG_LEVEL_INF);

LOG_INF("info message %d", value);
LOG_WRN("warning message");
LOG_ERR("error message");
```

## Debugging Tips

### Finding Code

```bash
# Search for a function/symbol
grep -rn "function_name" --include="*.c" --include="*.h" /testbed

# Use the ctags index (pre-built)
grep "function_name" /testbed/tags

# Find all files related to a subsystem
find /testbed/subsys/net -name "*.c" -o -name "*.h"
```

### Build Errors

- **Missing config**: Check `prj.conf` and `testcase.yaml` for required `CONFIG_*`
- **Undefined symbol**: Make sure headers are included and Kconfig enables the feature
- **Type mismatch**: Zephyr uses many typedefs — check `include/zephyr/` headers

### Understanding a Bug

1. Read the test that's failing — it shows what behavior is expected
2. Find the function under test in the source
3. Look at git blame to understand recent changes: `git log --oneline -20 path/to/file.c`
4. Compare with similar functions in the same file for patterns

### QEMU Notes

- QEMU targets (qemu_x86, etc.) run in an emulator — always use `run_tests`
- native_sim targets run as a host binary — they exit cleanly
- If QEMU hangs, the test likely hit an infinite loop in your fix
