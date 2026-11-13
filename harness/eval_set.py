#!/usr/bin/env python3
"""
What an evaluation of Mynewt, NuttX or Contiki-NG covers, and on which machine
architecture each instance may run.

    python harness/eval_set.py left --project nuttx --model qwen38-nvfp4 --arch aarch64
    python harness/eval_set.py summary --project nuttx --model qwen38-nvfp4

Zephyr's and RIOT's instances are published as images that run wherever the
job runs, so their set is simply every instance of the project
(paths.discover_instances), as eval_worker.py and results.py always used it.
Mynewt, NuttX and Contiki-NG instances are each built for one architecture
(metadata "host_arch", or Contiki-NG's "architecture"): an instance is taken
only by a job on that architecture, and one that no job of this architecture
can take stays "to do" in the results, listed by the other architecture's
count here, until a job of that architecture takes it. An instance that cannot
run on the cluster at all (NuttX's old Docker instances, which have no
Apptainer image) is not part of the set; `unrunnable` names them.

`left` prints how many instances of this architecture have no grade yet (what
a job's resubmission is decided on); `summary` prints the same by
architecture, for the job's log.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402

# Projects whose instance images are built where the evaluation runs
# (harness/eval_images.py) and that name the architecture they run on.
BUILT_HERE = ("mynewt", "nuttx", "contiki-ng")
ARCHS = ("aarch64", "x86_64")


def machine() -> str:
    """This machine's architecture, as instances name it."""
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine(), platform.machine())


def arch_of(meta: dict) -> str | None:
    """The architecture an instance runs on: host_arch (Mynewt, NuttX) or
    architecture (Contiki-NG); None when it names none."""
    return meta.get("host_arch") or meta.get("architecture")


def runnable(instance_id: str) -> str | None:
    """None if the instance can be evaluated on the cluster; else why not."""
    import projects
    try:
        meta = paths.load_metadata(instance_id)
    except FileNotFoundError:
        return "no metadata.json"
    try:
        runtime = projects.config_for(meta).get("runtime")
    except (ValueError, KeyError) as e:
        return str(e)
    return None if runtime == "apptainer" else f"runtime {runtime or 'docker'}, not an Apptainer image"


def classify(ids: list[str], project: str, arch: str | None = None) -> dict:
    """Sort `ids` of `project` for a job on `arch` (default: this machine):
    {"mine": the ones this job takes, "other": {arch: ids a job of that
    architecture takes}, "unrunnable": {id: why}}. For a project that is not
    BUILT_HERE every instance is "mine"."""
    ids = [i for i in ids if paths.split_instance(i)[0] == project]
    out = {"mine": [], "other": {}, "unrunnable": {}}
    for iid in ids:
        if project not in BUILT_HERE:
            out["mine"].append(iid)
        elif (why := runnable(iid)):
            out["unrunnable"][iid] = why
        else:
            want = arch_of(paths.load_metadata(iid))
            if want in (None, arch or machine()):
                out["mine"].append(iid)
            else:
                out["other"].setdefault(want, []).append(iid)
    return out


def instances(project: str, listed: str | None = None) -> list[str]:
    """The evaluation's instances of `project` on every architecture: those in
    the file `listed` (a trial), else all. For a project that is BUILT_HERE,
    only those that can run on the cluster."""
    ids = (Path(listed).read_text().split() if listed else paths.discover_instances(project))
    ids = [i for i in ids if paths.split_instance(i)[0] == project]
    if project not in BUILT_HERE:
        return ids
    return [i for i in ids if runnable(i) is None]


def left(project: str, model: str, arch: str | None = None, listed: str | None = None) -> dict:
    """Instances with no grade yet, for a job on `arch`: {"mine": [...],
    "other": {arch: [...]}, "unrunnable": {...}}."""
    model = paths.resolve_model(model)
    ids = (Path(listed).read_text().split() if listed else paths.discover_instances(project))
    c = classify(ids, project, arch)
    todo = lambda xs: [i for i in xs if not paths.grade_path(i, model).exists()]   # noqa: E731
    return {"mine": todo(c["mine"]), "other": {a: todo(xs) for a, xs in c["other"].items()},
            "unrunnable": c["unrunnable"]}


def describe(project: str, model: str, arch: str | None = None, listed: str | None = None) -> str:
    c = left(project, model, arch, listed)
    parts = [f"{len(c['mine'])} to grade on {arch or machine()}"]
    parts += [f"{len(xs)} to grade on {a} (not taken by a job of this architecture)"
              for a, xs in sorted(c["other"].items())]
    if c["unrunnable"]:
        parts.append(f"{len(c['unrunnable'])} cannot run on the cluster (not counted)")
    return "; ".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=["left", "summary", "json"])
    p.add_argument("--project", required=True, choices=paths.PROJECTS)
    p.add_argument("--model", required=True)
    p.add_argument("--arch", choices=ARCHS, help="default: this machine's")
    p.add_argument("--instances", help="file of instance ids (a trial)")
    a = p.parse_args()
    if a.what == "left":
        print(len(left(a.project, a.model, a.arch, a.instances)["mine"]))
    elif a.what == "summary":
        print(describe(a.project, a.model, a.arch, a.instances))
    else:
        print(json.dumps(left(a.project, a.model, a.arch, a.instances), indent=2))


if __name__ == "__main__":
    main()
