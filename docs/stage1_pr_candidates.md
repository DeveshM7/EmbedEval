# Stage 1 Zephyr PR candidates

## Six PRs that should be rejected

### [#99824 — STM32 external SDRAM configuration](https://github.com/zephyrproject-rtos/zephyr/pull/99824)

**Evidence**

- The production changes modify SDRAM size or FMC timing in the board descriptions for `stm32f746g_disco`, `stm32f7508_dk`, `stm32h745i_disco`, and `stm32h750b_dk`.
- The PR adds MEMC test overlays for ten physical STM32 development boards and modifies `tests/drivers/memc/ram/src/main.c`.
- The added `test_ram0` case obtains the address and size of `ram0` from the selected board's devicetree, writes across that memory, and verifies that every value can be read back.
- The [test configuration](https://github.com/zephyrproject-rtos/zephyr/blob/6b2e014292f25f2172b1c9d73d9cd650a4512012/tests/drivers/memc/ram/testcase.yaml) declares `depends_on: memc`. Neither [`qemu_x86`](https://github.com/zephyrproject-rtos/zephyr/blob/6b2e014292f25f2172b1c9d73d9cd650a4512012/boards/qemu/x86/qemu_x86.yaml) nor [`native_sim`](https://github.com/zephyrproject-rtos/zephyr/blob/6b2e014292f25f2172b1c9d73d9cd650a4512012/boards/native/native_sim/native_sim_native_64.yaml) declares that capability.

**Why it should be rejected**

The regression test requires the affected STM32 board's FMC controller and external SDRAM. Running a generic memory test on a different simulated target would not exercise the board descriptions changed by this PR, so the benchmark could not demonstrate fail-before/pass-after behavior on QEMU or `native_sim`.

**Expected rejection point:** Model triage after the hard filter. The current hard filter accepts it because it changes both source and executable tests.

### [#113956 — Raspberry Pi Pico timer fix without tests](https://github.com/zephyrproject-rtos/zephyr/pull/113956)

**Evidence**

- The merged PR changes only `drivers/counter/counter_rpi_pico_pit_channel.c`.
- The change prevents `pwm_init()` from resetting the timer clock divider when the top value is updated.
- No file under `tests/` and no executable assertion is added or modified.

**Why it should be rejected**

It is a production bug fix, but the PR does not provide a regression test that can be separated from the fix and used to evaluate an agent-generated patch.

**Expected rejection point:** Hard filter — `touches no tests/`.

### [#118399 — USB controller fix without a changed test](https://github.com/zephyrproject-rtos/zephyr/pull/118399)

**Evidence**

- The merged diff contains only `drivers/usb/uhc/uhc_dwc2.c` and `drivers/usb/uhc/uhc_dwc2.h`.
- The PR description discusses regression coverage, but the merged PR does not add or modify an executable test.

**Why it should be rejected**

Existing CI coverage or a test mentioned in the description cannot provide the test patch required to construct an EmbedEval instance. The detecting test must be changed in the same merged PR.

**Expected rejection point:** Hard filter — `touches no tests/`.

### [#110975 — revert that removes test coverage](https://github.com/zephyrproject-rtos/zephyr/pull/110975)

**Evidence**

- The title is `Revert "kernel: mutex: detect re-init and uninitialized use via magic sentinel"`.
- The PR changes production code in the kernel and modifies mutex and condition-variable tests.
- Its change to `tests/kernel/mutex/mutex_error_case/src/test_mutex_error.c` removes the tests for uninitialized and reinitialized mutexes. It does not retain or add a regression test that detects the tree breakage motivating the revert.

**Why it should be rejected**

Although test files are touched, the test changes remove coverage associated with the reverted behavior. There is no retained bug-specific test that can be applied to the pre-fix revision as the benchmark oracle.

**Expected rejection point:** Hard filter — the current script rejects titles beginning with `Revert`.

### [#112110 — test-suite work without a production change](https://github.com/zephyrproject-rtos/zephyr/pull/112110)

**Evidence**

- The PR adds `tests/arch/common/cache/src/test_cache_api.c`, removes the previous copy under `tests/kernel/cache/`, and moves or updates supporting test configuration files.
- The only changed path outside `tests/` is `MAINTAINERS.yml`.
- No production implementation is changed.

**Why it should be rejected**

The PR reorganizes and expands tests, but it gives the benchmark agent no missing or incorrect production behavior to implement. Applying its tests to the earlier source revision does not form a software-engineering repair task.

**Expected rejection point:** Model triage after the hard filter. The current hard filter counts `MAINTAINERS.yml` as source, so this PR presently passes the file checks.

### [#117561 — documentation-only correction](https://github.com/zephyrproject-rtos/zephyr/pull/117561)

**Evidence**

- The only changed file is `doc/services/connectivity/usb/device_next/usb_device.rst`.
- The PR corrects documentation and build-command wording without changing production code or executable tests.

**Why it should be rejected**

There is no production behavior for an agent to implement and no executable regression test with which to evaluate a patch.

**Expected rejection point:** Hard filter — `touches no tests/`.

## Good PR

### [#98142 — Zbus asynchronous listeners](https://github.com/zephyrproject-rtos/zephyr/pull/98142)

**Evidence**

- The PR adds asynchronous-listener behavior to the Zbus API and implementation in `include/zephyr/zbus/zbus.h`, `subsys/zbus/Kconfig`, and `subsys/zbus/zbus.c`.
- It adds a complete executable test application under `tests/subsys/zbus/async_listeners/`.
- `test_01_specification` checks message delivery from thread and interrupt contexts, burst delivery, ordering, and queue exhaustion behavior.
- `test_02_isolated_wqueue` checks that a listener can run on a selected work queue, receives the expected message contents, and executes on the expected thread.
- The [test configuration](https://github.com/zephyrproject-rtos/zephyr/blob/9463d9a51d9cb1094bf98ef437a39850a7b5705d/tests/subsys/zbus/async_listeners/testcase.yaml) selects `qemu_x86` and does not require physical hardware or an external service.

**Why it is good**

Feature additions are valid benchmark tasks when their tests detect the required behavior. These tests directly use the new asynchronous-listener API and assert its message-delivery and work-queue behavior. Without the feature implementation, the test application cannot build or satisfy those assertions; with the implementation, it has a deterministic QEMU execution path. The current hard filter accepts this PR.
