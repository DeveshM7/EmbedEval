# NuttX on QEMU (`qemu-armv8a`): prototype

Most NuttX instances run ostest on the native simulator `sim:nsh`. Some
pairs cannot: their kernel change exists only for a real architecture. This
prototype ran ostest on the arm64 QEMU board `qemu-armv8a` (configs `nsh` and
`nsh_smp`) in Apptainer on Scholar's ARM64 Spark nodes. It has since been
wired into the generator, validator and cluster pipeline:
`scripts/nuttx_platforms.py` maps each QEMU target to its image and command
line, and apps #2137 is a promoted instance (`nuttx__nuttx-10934-a2137`, with
#10970 in `extra_fix_prs`). This page keeps the prototype's findings.

Files, all in `templates/nuttx/qemu-armv8a/`:

| file | purpose |
|---|---|
| `nuttx-qemu.def` | Apptainer image: Ubuntu 24.04, NuttX build tools, Arm's `aarch64-none-elf` 12.3.rel1, `qemu-system-aarch64` (8.2.2) |
| `run_tests.sh.tmpl` | boots `/testbed/nuttx`, runs `ostest`, classifies the log like the sim runner; exit 0/1/2 |
| `validate_qemu.sbatch` | the fail-before / pass-after check of one pair, as `validate_instance.py` does it |
| `cases/nuttx__nuttx-10934/` | the proof case: `vars.sh` (commits, config, banners) and `test_patch.diff` |

## Result: apps #2137 / kernel #10934 is VALID on `qemu-armv8a:nsh_smp`

[apps #2137](https://github.com/apache/nuttx-apps/pull/2137) adds
`testing/ostest/smp_call.c` (gated by `CONFIG_SMP_CALL`).
[Kernel #10934](https://github.com/apache/nuttx/pull/10934) adds
`nxsched_smp_call_single()`/`nxsched_smp_call()` and `up_send_smp_call()`,
which exists only for arm64. `docs/nuttx_bad_prs.md` rejects this pair for
`sim:nsh` because the fixed kernel does not link there.

Slurm job 476984 (scholar-k060, 2026-10-16) ended with
`RESULT: VALID on qemu-armv8a (fails before the fix, passes after 3/3)`:

- **Before the fix:** the build fails with the expected missing API:
  `smp_call.c:64:7: error: implicit declaration of function 'nxsched_smp_call_single' [-Werror=implicit-function-declaration]`
  (and the same error for `nxsched_smp_call`). The recipe is `fail_type`
  `compile_error`, `compile_test_source` `testing/ostest/smp_call.c`,
  `missing_api` `nxsched_smp_call_single`.
- **After the fix:** `run_tests` exits 0 on 3 of 3 runs. Each log has
  `user_main: smp call test`, `smp_call_test: Test success`,
  `user_main: Exiting` and `ostest_main: Exiting with status 0`, and no `ERROR`.

**The fix needs two kernel PRs.** With #10934 alone (its branch range
`495253385f..ed686f0c8e`), ostest hangs in the new test after
`smp_call_test: Call cpu 1, nowait` and `run_tests` times out (job 476972, 1
run, 600 s). This is a kernel bug, not a QEMU problem. A nowait call whose
cpuset holds only the calling CPU takes `g_smp_call_data.lock`. No remote CPU
then runs the handler that releases it, so the next nowait call spins
forever. [Kernel #10970](https://github.com/apache/nuttx/pull/10970) ("smp
call exit immediately when cpuset change to 0") fixes this; its PR body says
"Testing: qemu-armv8a:nsh_smp smp_call test". It merged at 13:12 UTC on
2023-10-18, before the apps PR (14:20 UTC). `vars.sh` applies its single
commit `083c87b39a` after the #10934 range (`EXTRA_FIX_COMMITS`).

**Config.** The only override is `CONFIG_SMP_CALL=y`. It is appended after
`make olddefconfig`, as the Docker template's `EXTRA_CONFIG_RUN` does, because
the base kernel has no `SMP_CALL` symbol and olddefconfig would drop it. NuttX
`make` does not re-run olddefconfig, so the old kernel builds `smp_call.c` and
fails to compile it. The earlier review recipe listed more settings. In this
tree they already come from `nsh_smp`: it sets `SMP=y`, and `SMP` selects
`SPINLOCK`, `SCHED_RESUMESCHEDULER` and `IRQCOUNT`. `SMP_NCPUS=4` and
`SMP_DEFAULT_CPUSET=0xffffffff` are Kconfig defaults. The job log shows all of
them in the final `.config`.

**Commits** (PR branch base and head, as the generator uses them): kernel base
`495253385f55`, kernel fix `ed686f0c8e5c` + `083c87b39ac8`, apps base
`f117860a16da`. The apps head `2ff7ca4c` is not in the Scholar clone.
`test_patch.diff` is `gh pr diff 2137`, which is identical (apart from `index`
lines) to the merge commit's `ef86a99^..ef86a99` for `testing/ostest`.

## Findings

### Toolchain

`arch/arm64/src/Toolchain.defs` sets `CROSSDEV ?= aarch64-none-elf-` and builds
with `-Werror`. NuttX CI in 2023 used Arm's prebuilt **12.3.rel1**
(`tools/ci/docker/linux/Dockerfile` at the base commit). Arm publishes it for
aarch64 hosts too:
`https://developer.arm.com/-/media/Files/downloads/gnu/12.3.rel1/binrel/arm-gnu-toolchain-12.3.rel1-aarch64-aarch64-none-elf.tar.xz`.
The recipe picks the file with `$(uname -m)`, so the same recipe gets the
x86_64 build on an x86 host. The download is 116 MB, and the image is
439 MB in total.

**Native Ubuntu gcc does not work as-is.** `make CROSSDEV=` with Ubuntu 24.04's
gcc 13.3 compiles, but the link fails with
`undefined reference to '__stack_chk_guard'` / `'__stack_chk_fail'`, because
Ubuntu's gcc enables the stack protector by default (probe job 476980). Extra
flags might get further. I did not pursue that: the Arm toolchain is the
configuration NuttX itself tests.

The Docker base (`docker/bases/nuttx.Dockerfile`) installs
`gcc-aarch64-linux-gnu` with the comment "Covers: qemu-armv8a:nsh". That is not
the `aarch64-none-elf-` prefix the build looks for, and Ubuntu's cross gcc
probably has the same stack-protector default. This is untested; check it
before relying on the Docker base for this board.

### Configuration

`./tools/configure.sh -a ./apps qemu-armv8a:nsh_smp` (or `:nsh`), then the sim
template's edits (`TESTING_OSTEST=y`, `NSH_CONSOLE_LOGIN=n`, `NDEBUG=n`), plus
one more, `CONFIG_DEBUG_SCHED_INFO=n`, then `make olddefconfig`. Both
defconfigs already set `TESTING_OSTEST=y` and `DEBUG_ASSERTIONS=y`, so
`NDEBUG` defaults off. They leave `NSH_CONSOLE_LOGIN` unset.

Why `DEBUG_SCHED_INFO=n`: the qemu-armv8a defconfigs turn on scheduler info
logging, about 15,700 log lines per ostest run, against about 900 without it.
On SMP, the `[CPUn] ...` lines from several CPUs interleave character by
character with ostest's own output. With it on, 2 of 3 otherwise-passing runs
were classified as failures (job 476979). The reason was cond_test's benign
line `thread_signaler: ERROR waiter state = 1 != COND_WAITING`, garbled to
e.g. `ptthread_sihreadgna_mutex_timleedlockr: ERROR waiter state`. The
interleaving could just as easily garble a pass marker, and 15k lines is a lot
for an agent to read. Scheduler errors and warnings stay on.

`run_tests` also ignores lines containing `ERROR waiter state`.
`testing/ostest/cond.c` documents this as an SMP race (the signaler runs
before the waiter), and the line only increments `signaler_state`: it does not
count as an error or change ostest's exit status. It appeared 3 to 11 times
per run with sched info on, and 0 times in the 3 runs with it off. Any other
`ERROR` still fails the run.

The `nsh` config (not `nsh_smp`) sets `CONFIG_LIBC_FDT`, whose first build
downloads `dtc.zip` from GitHub and needs `unzip` (now in the image). An
instance image would have to do that first build at image-build time, so that
agent runs need no network.

### QEMU command line

This is NuttX's documented command (`boards/arm64/qemu/qemu-armv8a/README.txt`,
GICv3), with `-smp` set from `CONFIG_SMP_NCPUS` (1 for `nsh`, 4 for `nsh_smp`):

```sh
qemu-system-aarch64 -cpu cortex-a53 -smp 4 -nographic \
    -machine virt,virtualization=on,gic-version=3 -net none \
    -chardev stdio,id=con,mux=on -serial chardev:con \
    -mon chardev=con,mode=readline -kernel ./nuttx
```

### Console driving and exit

The sim runner pipes `ostest\npoweroff\n`. That does not carry over:
qemu-armv8a has no `board_power_off()` in this era (`psci_sys_poweroff()`
exists but nothing calls it), so nsh has no `poweroff`. `run_tests` therefore
does the following:

1. Starts QEMU in the background with its stdin from a FIFO and its output to
   a log file, wrapped in `timeout -k 10 $TIMEOUT` (default 600 s).
2. Polls the log once a second. At the first `nsh> ` it types `ostest`. At the
   second `nsh> ` (ostest returned to nsh) it sends `Ctrl-A x`, the mux
   console's quit key, and QEMU prints `QEMU: Terminated` and exits 0.
3. Stops early on `Assertion failed`. A NuttX assertion halts the system
   instead of returning to nsh, and without this check it would look like a
   timeout. This branch was not exercised: none of the runs hit an assertion.
4. Classifies the log exactly as the sim runner does: `user_main: Exiting`,
   `ostest_main: Exiting with status 0`, no `ERROR` (except the line above),
   and each changed test's `user_main: ... test` banner. Exit 0 when all hold,
   1 otherwise, 2 when `timeout` killed QEMU (exit 124/137). It also prints
   `run_tests: boot Ns, ostest Ns`.

**Hard timeout:** `timeout -k 10` kills QEMU itself. Each command also runs in
its own `apptainer exec --containall` PID namespace, so nothing outlives the
command, which is the same guarantee `harness/apptainer_env.py` relies on for
Zephyr. The timeout path was exercised in job 476972: QEMU reported
`terminating on signal 15 from pid 20 (timeout)` and the run returned 2.

### KVM

The Spark nodes (Cortex-X925/A725, 20 CPUs) have `/dev/kvm`, but it is
`crw-rw---- root kvm`, and the account is not in group `kvm`. `--containall`
also does not expose it. Everything runs under **TCG** emulation. KVM would
also need `-cpu host` without `virtualization=on`, i.e. the `*_hv` configs. It
is not needed: TCG boots in about 1 s.

## Costs (Scholar Spark node, TCG)

| step | time |
|---|---|
| image build (`apptainer build --fakeroot`, once) | 236-268 s |
| clone kernel + apps from the local bare clones | 37-44 s |
| `configure.sh` + `olddefconfig` | 26-31 s |
| cold build, `nsh` | 19 s |
| `nsh_smp`: base build to the compile error | 16-18 s; then the fixed build 10 s |
| boot to the first `nsh>` | about 1 s |
| full ostest, `nsh_smp` (4 vCPUs), fixed kernel | 114-117 s (6 runs; 3 with sched info off) |
| full ostest, `nsh` (1 vCPU), base kernel | 116 s (2 runs, both pass) |
| whole validation job (image reused) | 7.5 min |

ostest takes about the same time on 1 and 4 vCPUs, and with or without sched
logging, so its own sleeps and timeouts dominate, not emulation speed. The
default 600 s `run_tests` timeout is about 5x a normal run. The harness's
NuttX `command_timeout` (600 s) covers one run plus a build.

## Reproducing

```sh
# on the login node; inputs are this directory, synced to $T/proto
T=/scratch/scholar/$USER/nuttx-qemu-dev
rsync -a templates/nuttx/qemu-armv8a/ scholar.rcac.purdue.edu:$T/proto/
sbatch $T/proto/validate_qemu.sbatch nuttx__nuttx-10934   # log: $T/job-<id>.log
```

The job builds `$T/nuttx-qemu.sif` if it is missing and reads the full clones in
`/scratch/scholar/$USER/repos/`. `RUN_TIMEOUT` (environment) overrides the
600 s run cap. Logs of the runs above are in
`/scratch/scholar/zhan5136/nuttx-qemu-dev/` (`job-476972.log`: the #10934-only
hang; `job-476979.log`: sched info on; `job-476984.log`: VALID;
`job-476980.log`: `nsh` and native gcc probe).
