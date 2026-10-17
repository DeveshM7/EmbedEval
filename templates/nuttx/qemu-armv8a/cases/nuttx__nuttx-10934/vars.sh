# apps PR #2137 (ostest: Add smp function call test case) and kernel PR
# #10934 (sched: support smp function call). The test calls
# nxsched_smp_call_single/nxsched_smp_call, which the kernel PR adds; its
# up_send_smp_call exists only for arm64, so the pair needs qemu-armv8a.
# Commits are the PR branches' base and head, as the generator uses them
# (the kernel merge commit is 48ca996ad7, the apps one ef86a99; the apps head
# 2ff7ca4c is not in the clone, and test_patch.diff is `gh pr diff 2137`,
# identical to ef86a99^..ef86a99 apart from index lines).
KERNEL_BASE_COMMIT=495253385f55f13a235ca21b838fb3fee0cb85e4
KERNEL_MERGE_COMMIT=ed686f0c8e5cb1ef2f965b5b501f70a011a378f5
APPS_BASE_COMMIT=f117860a16da9966e35e0c0459facf31ac4e0dc4
FIX_FILES='arch/arm64/src/common/arm64_gicv3.c include/nuttx/arch.h include/nuttx/sched.h sched/Kconfig sched/sched/CMakeLists.txt sched/sched/Make.defs sched/sched/sched_smp.c'
# #10934 alone deadlocks the test: a nowait call to only the calling CPU
# takes g_smp_call_data.lock and never releases it (no remote CPU runs the
# handler that unlocks), so the next nowait call spins forever. Kernel PR
# #10970 (merged 2023-10-18 13:12 UTC, before the apps PR at 14:20; "Testing:
# qemu-armv8a:nsh_smp smp_call test") fixes that; its one commit is applied
# after the #10934 range, restricted to FIX_FILES.
EXTRA_FIX_COMMITS=083c87b39ac808488817ff7b1660ed7c1528cc0a
BOARD_CONFIG=qemu-armv8a:nsh_smp
# Appended after olddefconfig, as the Docker template's EXTRA_CONFIG_RUN does:
# the base kernel has no SMP_CALL symbol, and olddefconfig would drop it.
# nsh_smp already has SMP=y, and SMP selects SPINLOCK, SCHED_RESUMESCHEDULER
# and IRQCOUNT; SMP_NCPUS (4) and SMP_DEFAULT_CPUSET (0xffffffff) are defaults.
CONFIG_OVERRIDES='CONFIG_SMP_CALL=y'
BANNERS="'user_main: smp call test'"
FAIL_TYPE=compile_error
MISSING_API=nxsched_smp_call_single
