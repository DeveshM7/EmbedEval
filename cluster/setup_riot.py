#!/usr/bin/env python3
"""
One-time setup for RIOT, and a check that it is in place. Safe to run again:
it only creates what is missing, and never overwrites anything.

    python cluster/setup_riot.py                 # submits the job, from any node
    python cluster/setup_riot.py --dry-run       # writes the job, submits nothing

RIOT's native board runs only on x86, so this runs as a job on the x86
partition (config `riot.slurm`). Creates, under <shared>/riot/:

    venv/                        x86 Python environment (requirements.txt)
    riot.git                     full bare clone of RIOT
    setup/                       the job scripts and their logs

and <shared>/images/riotbuild-<release>.sif, RIOT's own build image of each
release in `riot.images`. Then checks, on the x86 node: the architecture, that
the pipeline's modules import, and that each image starts.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
sys.path[:0] = [str(CLUSTER), str(CLUSTER.parent / "scripts")]
import config as cluster_config  # noqa: E402

JOB = "embedeval-riot-setup"
PULLS = 3                 # images downloaded at once
results: list[tuple[str, str, str]] = []      # (status, item, detail)


def report(status: str, item: str, detail: str = "") -> None:
    results.append((status, item, detail))
    print(f"  [{status:^7}] {item}{': ' + detail if detail else ''}", flush=True)


def submit(cfg: dict, config: str, dry_run: bool) -> None:
    d, s = cfg["derived"], cfg["riot"]["slurm"]
    q = subprocess.run(["squeue", "-h", "--me", "-n", JOB, "-o", "%i %T"],
                       capture_output=True, text=True)
    if q.stdout.strip():
        sys.exit(f"the RIOT setup job is already there ({q.stdout.strip()})")
    folder = Path(d["riot"]) / "setup"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    script = folder / f"setup-{stamp}.sbatch"
    script.write_text((CLUSTER / "templates" / "riot_setup.sbatch").read_text().format(
        partition=s["partition"], riot=d["riot"], stamp=stamp, repo=CLUSTER.parent,
        python=cfg["riot"]["python"], config=config))
    print(f"  job script: {script}")
    if dry_run:
        print("  --dry-run: not submitted")
        return
    r = subprocess.run(["sbatch", str(script)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"sbatch failed: {r.stderr.strip()}")
    print(f"  {r.stdout.strip()}; log in {folder}")


def pull(tag: str) -> tuple[str, str, str]:
    """One image, whole or not at all."""
    import apptainer_util as au
    image = au.riot_image(tag)
    if image.exists():
        return "exists", image.name, ""
    part = au.path("images", f".{image.name}.partial")
    # Apptainer's download cache is of no use once the image exists (it reached
    # 44 GB for 7 images): its own folder here, emptied by on_node.
    env = {**au.build_env(au.path("tmp", f"pull-{tag}", "x").parent),
           "APPTAINER_CACHEDIR": str(au.path("cache", "x").parent)}
    r = subprocess.run(["apptainer", "pull", "--force", str(part),
                        f"docker://riot/riotbuild:{tag}"],
                       env=env, capture_output=True, text=True, timeout=3 * 3600)
    if r.returncode:
        if part.exists():
            au.remove(part)
        return "FAIL", image.name, (r.stderr or r.stdout).strip()[-200:]
    part.replace(image)
    return "pulled", image.name, f"{image.stat().st_size / 2**30:.1f} GB"


def riot_clone() -> None:
    import riot_env
    clone = riot_env.clone()
    if clone.exists():
        report("ok", "RIOT clone", f"exists, {clone}")
        return
    tmp = clone.with_name(clone.name + ".partial")
    r = subprocess.run(["git", "clone", "--bare", "-q", riot_env.URL, str(tmp)],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode:
        report("FAIL", "RIOT clone", (r.stderr or r.stdout).strip()[-200:])
        return
    tmp.rename(clone)                  # only a complete clone gets the real name
    report("ok", "RIOT clone", f"created, {clone}")


def on_node(cfg: dict) -> None:
    """The part that runs on the x86 node, in the x86 environment."""
    import os
    os.environ.update(cluster_config.env_for(cfg))
    # the only folder the guarded au.remove may delete in, for this setup
    os.environ["EMBEDEVAL_WORK"] = str(Path(cfg["derived"]["riot"]) / "work")
    import apptainer_util as au
    arch = platform.machine()
    report("ok" if arch == "x86_64" else "FAIL", "architecture", arch)
    report("ok", "python", platform.python_version())
    for name in ("claims", "ledger", "status", "report", "node_worker", "registry", "instances"):
        try:
            __import__(name)
            report("ok", f"import {name}")
        except Exception as e:
            report("FAIL", f"import {name}", str(e)[:120])
    riot_clone()
    tags = cfg["riot"]["images"]
    with ThreadPoolExecutor(PULLS) as pool:
        for status, name, detail in pool.map(pull, tags):
            report("FAIL" if status == "FAIL" else "ok", name, status + (f", {detail}" if detail else ""))
    for leftover in ("tmp", "cache", "images"):
        au.remove(au.work_root() / leftover)
    for tag in tags:
        image = au.riot_image(tag)
        if not image.exists():
            continue
        r = subprocess.run([*au.exec_args(image), "bash", "-c",
                            "gcc --version | head -1; python3 --version"],
                           capture_output=True, text=True, timeout=600)
        report("ok" if r.returncode == 0 else "FAIL", f"riotbuild {tag} starts",
               " / ".join(r.stdout.split("\n")[:2]) if r.returncode == 0 else r.stderr.strip()[-200:])
    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} ok, {len(failed)} failed"
          + ("" if not failed else ": " + "; ".join(r[1] for r in failed)))
    sys.exit(1 if failed else 0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="default: cluster/config.yaml, else the example's defaults")
    p.add_argument("--dry-run", action="store_true", help="write the job script, do not submit")
    p.add_argument("--on-node", action="store_true", help="(the job itself)")
    args = p.parse_args()
    cfg = cluster_config.load_or_exit(args.config)
    if args.on_node:
        on_node(cfg)
    else:
        submit(cfg, str(Path(args.config or cluster_config.DEFAULT).resolve()), args.dry_run)


if __name__ == "__main__":
    main()
