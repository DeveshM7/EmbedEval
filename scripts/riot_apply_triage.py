#!/usr/bin/env python3
"""
The last steps of a RIOT run, after the LLM stage (cluster/templates/riot_llm.sbatch):
put its verdicts into the instances, then publish the accepted ones.

    python scripts/riot_apply_triage.py --run-dir DIR apply [--drop-unjudged]
    python scripts/riot_apply_triage.py --run-dir DIR publish      # needs gh and EMBEDEVAL_REGISTRY

apply: an accepted instance gets the LLM's problem statement and change type in its
metadata.json (no rebuild: neither is in the layer). A rejected one is moved, with its
layer, to DIR/instances-rejected/ and work/images/rejected/: set aside, not deleted.
--drop-unjudged moves the valid instances with no verdict too (e.g. a record too long for
the model's context). Running it again changes nothing.

publish: pushes each accepted instance's layer with scripts/registry.py, skipping those
already published.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402


def verdicts(run: Path) -> dict[str, dict]:
    out = {}
    for f in (run / "triage").glob("*.json"):
        if not f.name.endswith(".trace.json"):
            v = json.loads(f.read_text())
            out[f"riot__riot-{v['pr']}"] = v
    return out


def set_aside(run: Path, iid: str) -> None:
    (run / "instances-rejected").mkdir(exist_ok=True)
    os.rename(run / "instances" / iid, run / "instances-rejected" / iid)
    image = au.instance_image(iid)
    if image.exists():
        (image.parent.parent / "rejected").mkdir(exist_ok=True)
        os.rename(image, image.parent.parent / "rejected" / image.name)


def cmd_apply(run: Path, drop_unjudged: bool) -> None:
    v_all, applied, moved, unjudged = verdicts(run), 0, 0, []
    for d in sorted((run / "instances").iterdir()):
        report = d / "validation.json"
        if not report.exists() or not json.loads(report.read_text()).get("valid"):
            continue
        v = v_all.get(d.name)
        if v is None:
            unjudged.append(d.name)
            if drop_unjudged:
                set_aside(run, d.name)
                moved += 1
        elif v["verdict"] == "accept":
            meta = json.loads((d / "metadata.json").read_text())
            meta.update(problem_statement=v["problem_statement"], problem_statement_source="llm",
                        change_type=v.get("change_type"))
            tmp = d / ".metadata.json.tmp"
            tmp.write_text(json.dumps(meta, indent=2) + "\n")
            tmp.replace(d / "metadata.json")
            applied += 1
        else:
            set_aside(run, d.name)
            moved += 1
    print(f"{applied} statements applied, {moved} set aside in instances-rejected/")
    if unjudged:
        print(f"no verdict{' (set aside)' if drop_unjudged else ''}: {' '.join(unjudged)}")


def cmd_publish(run: Path) -> None:
    import registry
    v_all, bad = verdicts(run), 0
    for d in sorted((run / "instances").iterdir()):
        if v_all.get(d.name, {}).get("verdict") != "accept" or registry.published(d.name):
            continue
        try:
            msg = f"pushed ({registry.push(d.name)['seconds']} s)"
        except Exception as e:      # one failure must not stop the rest; run again to retry
            msg, bad = f"FAILED: {e}", bad + 1
        print(f"  {d.name}: {msg}", flush=True)
    sys.exit(1 if bad else 0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("apply").add_argument("--drop-unjudged", action="store_true")
    sub.add_parser("publish")
    args = p.parse_args()
    run = layout.run_dir()
    if not run:
        sys.exit("needs --run-dir")
    if args.command == "apply":
        cmd_apply(run, args.drop_unjudged)
    else:
        cmd_publish(run)


if __name__ == "__main__":
    main()
