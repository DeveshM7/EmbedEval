#!/usr/bin/env python3
"""
One-time setup for running EmbedEval on Scholar, and a check that everything
is in place. Safe to run again: it only creates what is missing, only reads
what exists, and never deletes or overwrites anything.

    python cluster/setup.py                  # with cluster/config.yaml
    python cluster/setup.py --config FILE

Run it from an interactive session on a Spark node (e.g. an OnDemand Spark
desktop): the nodes are ARM64, and the base images it builds must be built on
the architecture they run on.

Creates, under the config's scratch_root:

    shared/                  Python/SDK images, dry-run venvs, uv cache
    shared/zephyr.git        full bare clone of Zephyr (a few GB, once)
    shared/images/python-*   one base image per Python version in the config
    runs/                    where prepare.py puts each run

and checks: ARM64, Apptainer with --fakeroot builds, gh logged in, Slurm
access to the partition, the model weights and the vLLM image.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
sys.path[:0] = [str(CLUSTER), str(CLUSTER.parent / "scripts")]
import config as cluster_config  # noqa: E402

ZEPHYR_URL = "https://github.com/zephyrproject-rtos/zephyr.git"
results: list[tuple[str, str, str]] = []      # (status, item, detail)


def report(status: str, item: str, detail: str = "") -> None:
    results.append((status, item, detail))
    print(f"  [{status:^7}] {item}{': ' + detail if detail else ''}", flush=True)


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def check_machine(cfg: dict) -> None:
    arch = platform.machine()
    report("ok" if arch == "aarch64" else "FAIL", "architecture",
           arch + ("" if arch == "aarch64" else " -- run this on a Spark (ARM64) node"))
    r = run(["apptainer", "--version"])
    report("ok" if r.returncode == 0 else "FAIL", "apptainer", (r.stdout or r.stderr).strip()[:80])
    r = run(["gh", "auth", "status"])
    report("ok" if r.returncode == 0 else "FAIL", "gh (GitHub API for enrichment)",
           "logged in" if r.returncode == 0 else "not logged in -- run `gh auth login`")
    s = cfg["slurm"]
    r = run(["sbatch", "--test-only", "-p", s["partition"], "-N", "1", f"--gres={s['gres']}",
             "-t", "00:05:00", "--wrap", "true"])
    ok = r.returncode == 0
    report("ok" if ok else "FAIL", f"Slurm partition {s['partition']}",
           "accepts jobs" if ok else (r.stderr or r.stdout).strip()[-160:])


def check_model(cfg: dict) -> None:
    import json
    p = cfg["paths"]
    profile = cfg["model"]["profile"]
    profiles = json.loads((cluster_config.REPO_ROOT / "triage" / "models.json").read_text())
    if profile not in profiles:
        report("FAIL", f"model profile {profile}", "not in triage/models.json")
        return
    weights = Path(p["models"]) / profiles[profile]["weights"]
    found = (weights / "config.json").exists()
    report("found" if found else "FAIL", f"model weights ({profile})",
           str(weights) if found else f"missing {weights}: download the model there "
           "(see docs/running_on_scholar.md, 3.2)")
    img = Path(p["vllm_image"])
    report("found" if img.exists() else "FAIL", "vLLM image",
           str(img) if img.exists() else f"missing {img} (see docs/running_on_scholar.md, 3.2)")
    cache = Path(cfg["derived"]["vllm_cache"])
    warm = (cache / ".warm").exists()
    report("found" if warm else "note", "vLLM compile cache",
           str(cache) if warm else "not filled yet; the first server start fills it "
           "(about 8 min slower, once)")


def make_dirs(cfg: dict) -> None:
    d = cfg["derived"]
    for name in ("shared", "runs"):
        path = Path(d[name])
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        report("found" if existed else "created", f"{name}/", str(path))


def zephyr_clone(cfg: dict) -> None:
    clone = Path(cfg["derived"]["zephyr_clone"])
    if clone.exists():
        r = run(["git", "-C", str(clone), "rev-parse", "--is-bare-repository"])
        ok = r.returncode == 0 and r.stdout.strip() == "true"
        report("found" if ok else "FAIL", "Zephyr clone",
               str(clone) if ok else f"{clone} exists but is not a bare git clone")
        return
    print(f"  cloning {ZEPHYR_URL} into {clone} (full, a few GB; minutes) ...", flush=True)
    clone.parent.mkdir(parents=True, exist_ok=True)
    tmp = clone.with_name(clone.name + ".partial")
    if tmp.exists():
        report("FAIL", "Zephyr clone", f"{tmp} is left from an interrupted clone; "
               "remove it and run again")
        return
    r = run(["git", "clone", "--bare", "-q", ZEPHYR_URL, str(tmp)], timeout=3600)
    if r.returncode == 0:
        tmp.rename(clone)                      # only a complete clone gets the real name
        report("created", "Zephyr clone", str(clone))
    else:
        report("FAIL", "Zephyr clone", (r.stderr or r.stdout).strip()[-200:])


def base_images(cfg: dict) -> None:
    # Everything a build writes -- the image, Apptainer's cache and its
    # temporary build tree -- goes under the shared root, never elsewhere.
    os.environ["EMBEDEVAL_SHARED"] = cfg["derived"]["shared"]
    os.environ["EMBEDEVAL_WORK"] = cfg["derived"]["shared"]
    import apptainer_util as au
    logs = Path(cfg["derived"]["shared"]) / "logs"

    def one(py: str) -> None:
        img = au.python_image(py)
        if img.exists():
            report("found", f"base image Python {py}", img.name)
            return
        ok = au.build(img, au.TEMPLATES / "python.def", {"PYTHON": py},
                      logs / f"build-{img.stem}.log", timeout_min=15)  # ~5 min normally
        report("created" if ok else "FAIL", f"base image Python {py}",
               img.name if ok else f"build failed; see {logs}/build-{img.stem}.log "
               "(needs `apptainer build --fakeroot`)")

    with ThreadPoolExecutor(len(cfg["python_versions"]) or 1) as pool:
        list(pool.map(one, cfg["python_versions"]))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="default: cluster/config.yaml, else the example's defaults")
    args = p.parse_args()
    cfg = cluster_config.load_or_exit(args.config)
    print(f"EmbedEval setup -- config: {cfg['derived']['config_file'] or 'defaults (no config.yaml)'}\n"
          f"  scratch root: {cfg['derived']['root']}\n")
    check_machine(cfg)
    check_model(cfg)
    make_dirs(cfg)
    zephyr_clone(cfg)
    base_images(cfg)
    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} ok, {len(failed)} failed"
          + ("" if not failed else ": " + "; ".join(r[1] for r in failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
