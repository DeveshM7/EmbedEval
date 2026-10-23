#!/usr/bin/env python3
"""Render a Mynewt instance as an Apptainer recipe.

Takes the instance's metadata (the fields generate_instance.py's generate_mynewt
writes) and its selftest patch, and writes instance.def, run_tests.sh,
bench.json, test_patch.diff, metadata.json and, for ARM64 hosts, sim_arm64.patch
into <out>/<instance id>/. The recipe is built FROM the shared image of
templates/mynewt/apptainer/base.def by scripts/build_instances.py. An existing
Docker instance is re-rendered from its own files:

    python scripts/render_mynewt_instance.py \
        --metadata docker/instances/mynewt__mynewt-3299/metadata.json \
        --test-patch docker/instances/mynewt__mynewt-3299/test_patch.diff

Old trees. Before the March 2019 rename the unit tests are in test/ packages
(`pkg.type: unittest`), and the newt of that era must build them: the newest
release tag that is an ancestor of the base commit picks the newt binary the
shared image holds under /opt/newt/<version>/ (NEWT_FOR_LAST_RELEASE below:
last release 0.9 -> newt 1.0.0-b1, 1.0 beta 1 -> b2, 1.0 beta 2 to 1.1 ->
1.0.0, 1.2-1.3 -> 1.2.0, 1.4 -> 1.4.0, 1.5 -> 1.6.0, 1.6 and later -> the
pinned modern newt). Their
sources need `-Wno-error` and `-fcommon` under GCC 13, passed as target
cflags. These instances build -m32 (x86 only), so they get no ARM64 patch and
metadata says host_arch x86_64. Old test mains may return 0 on failures
(MYNEWT-934): run_tests judges the [pass]/[FAIL] lines, not the exit status.

ARM64. Mynewt's native simulator has no aarch64 code upstream: the context
switch (kernel/os/src/arch/sim/os_arch_stack_frame.s) is x86 assembly, and
before 2024-04-09 compiler/sim forced -m32 and the kernel assumed 32-bit
pointers. The renderer therefore also makes sim_arm64.patch, applied by the
recipe on aarch64 hosts only and committed into the base:

    base commit >= 2024-04-09   an aarch64 port of os_arch_frame_init
    base commit <  2024-04-09   the 64-bit series of that day (5218d9b93,
                                b5e9553b3, 05d803436, 456c0eb7f) and the port

The era is chosen from the base commit's date, read from a mynewt-core clone
(EMBEDEVAL_MYNEWT_CLONE, else candidates/mynewt.git) and recorded in metadata
as arm64_sim_port ("asm" or "asm+64bit"). The series' hunks that conflict with
the base are dropped; validate on an ARM64 host before relying on one.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apptainer_util as au  # noqa: E402
import instances  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates" / "mynewt"
DEFAULT_OUT = REPO_ROOT / "generated"

SIM_64BIT_DATE = "2024-04-09"
# Upstream's 64-bit sim series, oldest first (all authored 2024-04-09).
SIM_64BIT_SERIES = ("5218d9b93", "b5e9553b3", "05d803436", "456c0eb7f")
SIM_ASM = "kernel/os/src/arch/sim/os_arch_stack_frame.s"
META_KEYS = ("test_path", "baseline_tests", "fail_to_pass", "pass_to_pass",
             "failure_mode", "compile_test_source", "missing_api")

AARCH64_FRAME_INIT = """#if defined(__aarch64__)
    stp     x29, x30, [sp, #-16]!   /* function prologue for backtrace */
    mov     x29, sp
    mov     x9, sp
    str     x9, [x0]                /* sf->mainsp = sp */

    /*
     * Switch the stack so that the stack pointer saved in 'sf->sf_jb' points
     * to the task stack, just below 'sf'.
     */
    and     x9, x0, #-16            /* align sp on 16-byte boundary */
    mov     sp, x9
    stp     x0, xzr, [sp, #-16]!    /* keep sf across sigsetjmp() */
    add     x0, x0, #8              /* x0 = &sf->sf_jb */
    mov     x1, #0
    bl      CNAME(sigsetjmp)        /* sigsetjmp(sf->sf_jb, 0) */
    ldp     x9, x10, [sp], #16      /* x9 = sf */
    cbnz    w0, 1f
    ldr     x9, [x9]
    mov     sp, x9                  /* switch back to the main() stack */
    ldp     x29, x30, [sp], #16
    ret                             /* return to os_arch_task_stack_init() */
1:
    mov     w1, w0                  /* rc */
    mov     x0, x9                  /* sf */
    mov     x29, xzr
    bl      CNAME(os_arch_task_start) /* os_arch_task_start(sf, rc) */
    /* never returns */
#elif __SIZEOF_POINTER__ == 8
"""


# (lowest last-released core, newt version in the image), checked from the
# newest down. The last release is the newest mynewt_<a>_<b>_<c>[_b<n>]_tag
# committed no later than the base commit; the betas count as 0.9.9 and
# 0.9.99, as repository.yml numbers them. A tree is built by the newt of the
# release it is working towards, which is what these steps give (checked on
# PRs 114-1594 of 2016-2019: tests/ in scratch codex_oldera/REPORT.md).
NEWT_FOR_LAST_RELEASE = (((1, 6, 0), "modern"), ((1, 5, 0), "1.6.0"), ((1, 4, 0), "1.4.0"),
                         ((1, 2, 0), "1.2.0"), ((0, 9, 99), "1.0.0"), ((0, 9, 9), "1.0.0-b2"),
                         ((0, 0, 0), "1.0.0-b1"))
OLD_CFLAGS = ["-Wno-error", "-fcommon"]
RELEASE_TAG = re.compile(r"mynewt_(\d+)_(\d+)_(\d+)(?:_b(\d+))?_tag$")


def git(clone: Path, *args: str, cwd: Path | None = None, check: bool = True) -> str:
    cmd = ["git", *(["-C", str(cwd)] if cwd else ["-C", str(clone)]), *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()[:300]}")
    return r.stdout


def core_release(clone: Path, base: str) -> tuple[int, int, int]:
    """The newest release tag committed no later than the base commit, as a
    version tuple; (0, 0, 0) before the first. By date, not ancestry: release
    tags sit on release-branch commits that master does not contain."""
    when = int(git(clone, "show", "-s", "--format=%ct", base))
    best = (0, 0, 0)
    for line in git(clone, "tag", "-l", "mynewt_*_tag",
                    "--format=%(refname:strip=2) %(committerdate:unix) %(*committerdate:unix)").splitlines():
        name, *dates = line.split()
        m = RELEASE_TAG.match(name)
        if not m or int(dates[-1]) > when:
            continue
        version = tuple(int(x) for x in m.groups()[:3])
        if m[4]:                       # only the 1.0.0 betas matter; 0.8.0's do not
            if version != (1, 0, 0):
                continue
            version = (0, 9, 9 if m[4] == "1" else 99)
        best = max(best, version)
    return best


def newt_for_base(clone: Path, base: str) -> tuple[str, str]:
    """(newt version in the shared image, last core release it is based on)."""
    rel = core_release(clone, base)
    version = next(v for floor, v in NEWT_FOR_LAST_RELEASE if rel >= floor)
    return version, ".".join(map(str, rel))


def port_aarch64(source: str) -> str:
    """The sim's stack-frame assembly with an aarch64 branch added."""
    if "aarch64" in source:
        return source
    out = source.replace("#if __SIZEOF_POINTER__ == 8\n    .code64\n",
                         "#if defined(__aarch64__)\n#elif __SIZEOF_POINTER__ == 8\n    .code64\n", 1)
    out = out.replace("    .p2align 4, 0x90    /* align on 16-byte boundary and fill with NOPs */",
                      "#if defined(__aarch64__)\n    .p2align 4\n#else\n"
                      "    .p2align 4, 0x90    /* align on 16-byte boundary and fill with NOPs */\n#endif", 1)
    out = out.replace("CNAME(os_arch_frame_init):\n#if __SIZEOF_POINTER__ == 8\n",
                      "CNAME(os_arch_frame_init):\n" + AARCH64_FRAME_INIT, 1)
    if out.count("__aarch64__") < 3:
        raise RuntimeError(f"{SIM_ASM} has an unexpected layout; cannot add the aarch64 port")
    return out


def arm64_patch(clone: Path, base: str) -> tuple[str, str]:
    """(sim_arm64.patch text, "asm" | "asm+64bit") for a base commit."""
    date = git(clone, "show", "-s", "--format=%cs", base).strip()
    series = date < SIM_64BIT_DATE
    work = Path(tempfile.mkdtemp(prefix="mynewt-arm64-"))
    tree = work / "tree"
    try:
        git(clone, "worktree", "add", "-q", "--detach", str(tree), base)
        ident = ["-c", "user.name=EmbedEval", "-c", "user.email=embedeval@localhost"]
        if series:
            for sha in SIM_64BIT_SERIES:
                subprocess.run(["git", "-C", str(tree), "cherry-pick", "-n", sha],
                               capture_output=True, text=True)
                unmerged = git(clone, "diff", "--name-only", "--diff-filter=U",
                               cwd=tree).split()
                for f in unmerged:            # not needed by the native selftests
                    git(clone, "checkout", "HEAD", "--", f, cwd=tree)
                subprocess.run(["git", "-C", str(tree), "cherry-pick", "--quit"],
                               capture_output=True)
                git(clone, *ident, "commit", "-q", "-a", "--allow-empty", "-m", sha, cwd=tree)
        asm = tree / SIM_ASM
        asm.write_text(port_aarch64(asm.read_text()))
        return git(clone, "diff", base, cwd=tree), ("asm+64bit" if series else "asm")
    finally:
        subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", str(tree)],
                       capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


def render(meta: dict, patch_file: Path, out_root: Path, arm64: bool = True) -> Path:
    instance_id = meta["instance_id"]
    if meta["project"] != "mynewt" or instances.split_instance(instance_id)[0] != "mynewt":
        raise ValueError("expected a Mynewt instance ID")
    if not re.fullmatch(r"[0-9a-f]{40}", meta["base_commit"]):
        raise ValueError("base_commit must be a full commit SHA")
    for key in META_KEYS:
        if key not in meta:
            raise ValueError(f"metadata lacks {key}")
    if meta.get("platform", "native") != "native":
        raise ValueError("Mynewt platform must be native")
    inst = out_root / instance_id
    if inst.exists():
        raise FileExistsError(f"output already exists: {inst}")

    clone = au.mynewt_clone()
    newt, core = "modern", None
    if clone is not None:
        newt, core = newt_for_base(clone, meta["base_commit"])
    elif arm64:
        raise RuntimeError("no mynewt-core clone to read the base commit's era: set "
                           "EMBEDEVAL_MYNEWT_CLONE")
    old = newt != "modern"
    cflags = list(meta.get("compatibility_cflags", []))
    if old:
        cflags += [f for f in OLD_CFLAGS if f not in cflags]
    pkg = ["pkg.name: targets/unittest", "pkg.type: target"]
    if cflags:
        pkg += ["pkg.cflags:"] + [f"    - {f}" for f in cflags]

    arm_patch, arm_kind = "", None
    if arm64 and not old:
        arm_patch, arm_kind = arm64_patch(clone, meta["base_commit"])

    recipe = (TEMPLATES / "apptainer" / "instance.def.tmpl").read_text()
    recipe = recipe.replace("@@NEWT_STEP@@\n", (
        "    # The newt of this base's era (see render_mynewt_instance.py).\n"
        f"    ln -sf /opt/newt/{newt}/newt /usr/local/bin/newt\n    newt version\n") if old else "")
    arm_step = ""
    if arm_patch:
        arm_step = (
            "    # ARM64 host only: the sim has no aarch64 context-switch code upstream.\n"
            "    # The port is committed, so it is part of the base the agent starts\n"
            "    # from, never of its patch.\n"
            "    if [ \"$(uname -m)\" = aarch64 ]; then\n"
            "        git apply /opt/benchmark/sim_arm64.patch\n"
            "        git -c user.name=EmbedEval -c user.email=benchmark@localhost \\\n"
            "            commit -q -a -m \"sim: aarch64 host support\"\n"
            "    fi")
    for key, value in (
            ("INSTANCE_ID", instance_id), ("BASE_COMMIT", meta["base_commit"]),
            ("ARM_FILES", "    sim_arm64.patch /opt/benchmark/sim_arm64.patch" if arm_patch else ""),
            ("ARM_STEP", arm_step),
            ("PKG_YML", " ".join(f"'{line}'" for line in pkg))):
        recipe = recipe.replace(f"@@{key}@@", value)

    inst.mkdir(parents=True)
    (inst / "instance.def").write_text(recipe)
    shutil.copy(TEMPLATES / "run_tests.sh", inst / "run_tests.sh")
    (inst / "test_patch.diff").write_text(patch_file.read_text())
    (inst / "bench.json").write_text(json.dumps({k: meta[k] for k in META_KEYS}))
    if arm_patch:
        (inst / "sim_arm64.patch").write_text(arm_patch)
    out = {k: v for k, v in meta.items() if k not in ("docker_image", "docker_platform")}
    out.update(runtime="apptainer", apptainer_image=f"{instance_id}.sif", generated=True)
    out["newt_version"] = newt
    if core:
        out["core_release"] = core
    if old:
        out["host_arch"] = "x86_64"
    if arm_kind:
        out["arm64_sim_port"] = arm_kind
    (inst / "metadata.json").write_text(json.dumps(out, indent=4) + "\n")
    return inst


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--metadata", type=Path, required=True)
    p.add_argument("--test-patch", type=Path, required=True)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--no-arm64", action="store_true", help="do not make sim_arm64.patch")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    print(render(json.loads(args.metadata.read_text()), args.test_patch, args.out,
                 not args.no_arm64))


if __name__ == "__main__":
    main()
