#!/bin/bash
#SBATCH -A scholar
#SBATCH -p spark-batch
#SBATCH --gres=gpu:1
#SBATCH -N 1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=20
#SBATCH --mem=32G
#SBATCH -t 01:00:00
#SBATCH -J nuttx-qemu-validate
#SBATCH -o /scratch/scholar/%u/nuttx-qemu-dev/job-%j.log
#
# Usage: sbatch validate_qemu.sbatch nuttx__nuttx-10934   (inputs in cases/<id>/)
#
# The fail-before / pass-after check of a NuttX pair on qemu-armv8a, in
# Apptainer on a Spark (ARM64) node, with the steps of validate_instance.py:
# the PR's test FAILS at the kernel base commit and PASSES with the kernel
# fix. Each run_tests is repeated 3 times to catch flakiness. A compile_error
# pair fails before the fix at build time, so only its fixed runs boot QEMU.
#
# This directory (templates/nuttx/qemu-armv8a) is expected at $T/proto.
set -u
export PATH=$(printf "%s" "$PATH" | tr ":" "\n" | grep -v "^/apps/cent7" | paste -sd: -)

T=/scratch/scholar/$USER/nuttx-qemu-dev
P=$T/proto
REPOS=/scratch/scholar/$USER/repos
SIF=$T/nuttx-qemu.sif
W=$T/work-$SLURM_JOB_ID
ID=${1:?usage: sbatch validate_qemu.sbatch <instance id>}
# KERNEL_BASE_COMMIT KERNEL_MERGE_COMMIT APPS_BASE_COMMIT FIX_FILES BOARD_CONFIG
# CONFIG_OVERRIDES BANNERS FAIL_TYPE MISSING_API [EXTRA_FIX_COMMITS]
source "$P/cases/$ID/vars.sh"
RUN_TIMEOUT=${RUN_TIMEOUT:-600}
export APPTAINER_CACHEDIR=$T/cache APPTAINER_TMPDIR=$T/tmp-$SLURM_JOB_ID
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR" "$W/testbed"

log() { echo "[$(date +%T)] $*"; }
X() {  # run a command in the image, with the work tree at /testbed
    apptainer exec --containall --bind "$REPOS:/repos:ro,$P:/p:ro,$W:/w,$W/testbed:/testbed" \
        --pwd /testbed "$SIF" bash -c "$1"
}
timed() {  # timed <label> <logfile> <command>: run in the image, log output, print rc and time
    local t0=$SECONDS; X "$3" > "$W/$2" 2>&1; local rc=$?
    log "$1: rc=$rc in $((SECONDS - t0))s (log: work-$SLURM_JOB_ID/$2)"; return $rc
}
fail() { log "STOP: $*"; echo "RESULT: ERROR ($*)"; exit 1; }

log "$ID on node $(hostname), arch $(uname -m), $(nproc) cpus, apptainer $(apptainer --version)"
[ "$(uname -m)" = aarch64 ] || fail "not an ARM64 node"
log "kvm: $(ls -l /dev/kvm 2>&1)"

# 1. The image (built once; later jobs reuse it).
if [ ! -f "$SIF" ]; then
    t0=$SECONDS
    apptainer build --fakeroot "$SIF.partial" "$P/nuttx-qemu.def" > "$T/build-image.log" 2>&1 \
        && mv "$SIF.partial" "$SIF" || { tail -25 "$T/build-image.log"; fail "image build failed"; }
    log "image built in $((SECONDS - t0))s"
else
    log "image exists, reused"
fi
X 'echo "  $(aarch64-none-elf-gcc --version | head -1)"; echo "  $(qemu-system-aarch64 --version | head -1)"'

# 2. Kernel at /testbed, apps inside it, as in the Docker instance.
timed "checkout" checkout.log "git clone -q --no-checkout /repos/nuttx.git . && git checkout -q $KERNEL_BASE_COMMIT \
    && git clone -q --no-checkout /repos/nuttx-apps.git apps && git -C apps checkout -q $APPS_BASE_COMMIT" \
    || { tail -5 "$W/checkout.log"; fail "checkout"; }

# 3. Configure as templates/nuttx/Dockerfile.tmpl does, on the QEMU board,
# then append the pair's overrides after olddefconfig (its EXTRA_CONFIG_RUN).
# One addition: the qemu-armv8a defconfigs set CONFIG_DEBUG_SCHED_INFO, ~12k
# "[CPUn] ..." lines per ostest run; on SMP they interleave character by
# character with ostest's own output and can garble the lines run_tests
# reads. Scheduler errors and warnings stay on.
timed "configure $BOARD_CONFIG" configure.log "./tools/configure.sh -a ./apps $BOARD_CONFIG \
    && sed -i '/^CONFIG_TESTING_OSTEST=/d; /^# CONFIG_TESTING_OSTEST is not set/d; /^CONFIG_NSH_CONSOLE_LOGIN=/d; /^# CONFIG_NSH_CONSOLE_LOGIN is not set/d; /^CONFIG_NDEBUG=/d; /^# CONFIG_NDEBUG is not set/d; /^CONFIG_DEBUG_SCHED_INFO=/d; /^# CONFIG_DEBUG_SCHED_INFO is not set/d' .config \
    && printf '%s\n' 'CONFIG_TESTING_OSTEST=y' 'CONFIG_NSH_CONSOLE_LOGIN=n' 'CONFIG_NDEBUG=n' 'CONFIG_DEBUG_SCHED_INFO=n' >> .config \
    && make olddefconfig \
    && grep -q '^CONFIG_TESTING_OSTEST=y\$' .config \
    && grep -q '^# CONFIG_NDEBUG is not set\$' .config \
    && grep -q '^# CONFIG_DEBUG_SCHED_INFO is not set\$' .config" \
    || { tail -15 "$W/configure.log"; fail "configure"; }
for kv in $CONFIG_OVERRIDES; do
    k=${kv%%=*}
    sed -i "/^$k=/d; /^# $k is not set/d" "$W/testbed/.config"
    echo "$kv" >> "$W/testbed/.config"
done
log "config: $(grep -E '^(CONFIG_SMP|CONFIG_SMP_[A-Z_]*|CONFIG_SPINLOCK|CONFIG_SCHED_RESUMESCHEDULER|CONFIG_IRQCOUNT|CONFIG_TESTING_OSTEST|CONFIG_DEBUG_ASSERTIONS)=|NDEBUG|NSH_CONSOLE_LOGIN|DEBUG_SCHED_INFO' "$W/testbed/.config" | tr '\n' ' ')"

# 4. The PR's test change, then build and run at the base commit.
sed "s|@@BANNERS@@|$BANNERS|" "$P/run_tests.sh.tmpl" > "$W/run_tests.sh"
timed "apply test patch" apply-test.log "cd apps && git apply /p/cases/$ID/test_patch.diff" || fail "test patch"
BEFORE=""
if timed "build (base)" build-base.log 'make -j$(nproc)'; then
    for i in 1 2 3; do
        t0=$SECONDS; X "bash /w/run_tests.sh $RUN_TIMEOUT" > "$W/run-base-$i.log" 2>&1; rc=$?
        BEFORE+="$rc "; log "run_tests base #$i: rc=$rc in $((SECONDS - t0))s"
        grep '^run_tests:' "$W/run-base-$i.log" | sed 's/^/    /'
    done
else
    BEFORE="build-failed "
    grep -E 'error:|undefined reference' "$W/build-base.log" | sort -u | head -10 | sed 's/^/    /'
    if [ "${FAIL_TYPE:-}" = compile_error ] && grep -q -- "$MISSING_API" "$W/build-base.log"; then
        log "base build fails on the missing API $MISSING_API, as the recipe expects"
        BEFORE="compile_error "
    fi
fi

# 5. The kernel fix (validate_instance.py's gold_patch), rebuild, run again.
X "git -C /repos/nuttx.git diff $KERNEL_BASE_COMMIT..$KERNEL_MERGE_COMMIT -- $FIX_FILES" > "$W/fix.diff"
log "fix patch: $(grep -c '^diff --git' "$W/fix.diff") files, $(wc -l < "$W/fix.diff") lines"
timed "apply fix" apply-fix.log "git apply /w/fix.diff" || { cat "$W/apply-fix.log"; fail "fix patch"; }
# Follow-up kernel commits the test also needs (from later PRs), one by one.
for c in ${EXTRA_FIX_COMMITS:-}; do
    X "git -C /repos/nuttx.git diff $c^..$c -- $FIX_FILES" > "$W/fix-$c.diff"
    timed "apply follow-up $c" "apply-$c.log" "git apply /w/fix-$c.diff" \
        || { cat "$W/apply-$c.log"; fail "follow-up $c"; }
done
timed "build (fixed)" build-fixed.log 'make -j$(nproc)' \
    || { grep -E 'error:|undefined reference' "$W/build-fixed.log" | tail -15; fail "fixed build"; }
AFTER=""
for i in 1 2 3; do
    t0=$SECONDS; X "bash /w/run_tests.sh $RUN_TIMEOUT" > "$W/run-fixed-$i.log" 2>&1; rc=$?
    AFTER+="$rc "; log "run_tests fixed #$i: rc=$rc in $((SECONDS - t0))s"
    grep '^run_tests:' "$W/run-fixed-$i.log" | sed 's/^/    /'
done

log "before the fix: [ $BEFORE] (want compile_error, or 1 1 1), after: [ $AFTER] (want 0 0 0)"
if { [ "$BEFORE" = "compile_error " ] || [ "$BEFORE" = "1 1 1 " ]; } && [ "$AFTER" = "0 0 0 " ]; then
    echo "RESULT: VALID on qemu-armv8a (fails before the fix, passes after 3/3)"
else
    echo "RESULT: NOT VALID on qemu-armv8a"
fi
rm -rf "$APPTAINER_TMPDIR"
