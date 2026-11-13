#!/usr/bin/env python3
"""
Promote validated Zephyr and Mynewt instances from generated/ to apptainer/instances/.
Mynewt recipes are placed under apptainer/instances/mynewt/.

    python scripts/promote_instance.py zephyr__zephyr-65697
    python scripts/promote_instance.py --all            # every valid one in generated/

apptainer/instances/ is the committed Apptainer benchmark: the instances the
harness runs agents on. Only an instance that passed validation gets there,
and only as validated: its recipe files must still match the fingerprint
validate_instance.py recorded in validation.json. A recipe regenerated or
edited after validation is refused until it is validated again.

Copies the recipe and its record -- instance.def, instance.json,
run_tests.py, test_patch.diff, metadata.json, validation.json. Images are not
in git; build_instances.py rebuilds them from the recipe.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402
import instances  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED = layout.generated()
DEST = REPO_ROOT / "apptainer" / "instances"
RECORD_FILES = ("metadata.json", "validation.json")
# The saved build output a config or dependency oracle was attributed from
# (generate_instance.py); render_mynewt_instance.py needs it to re-render.
EVIDENCE_FILES = ("observed_before.log",)


def promote(instance_id: str, source: Path = GENERATED,
            host_arch: str | None = None) -> tuple[bool, str]:
    src = source / instance_id
    project = instances.split_instance(instance_id)[0]
    recipe = au.mynewt_recipe_files(src) if project == "mynewt" else au.RECIPE_FILES
    report_path = src / "validation.json"
    if not report_path.exists():
        return False, "not validated (no validation.json)"
    report = json.loads(report_path.read_text())
    if not report.get("valid"):
        return False, f"invalid: {report.get('reason')}"
    want = report.get("recipe_sha256")
    if not want:
        return False, "validation.json predates recipe fingerprints; validate it again"
    if au.recipe_fingerprint(src, recipe) != want:
        return False, "recipe changed since it was validated; validate it again"
    if project == "mynewt":
        # Nothing an agent must not see in the image (validate_instance.py's
        # leak check), and a before state judged the same in 5 runs.
        leak = report.get("leak_check")
        if not leak:
            return False, "no leak check record; validate it again"
        if not leak.get("ok") or leak.get("network") != "isolated":
            return False, f"leak check failed: {leak.get('findings')}"
        d = report.get("determinism")
        if not d or d.get("runs", 0) < 5 or not d.get("ok"):
            return False, "no deterministic before state (validate with --determinism 5)"
    files = (*recipe, *RECORD_FILES, *[f for f in EVIDENCE_FILES if (src / f).exists()])
    missing = [f for f in files if not (src / f).exists()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    dst = (DEST / "mynewt" if project == "mynewt" else DEST) / instance_id
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(src / f, dst / f)
    if host_arch:
        meta = json.loads((dst / "metadata.json").read_text())
        meta["host_arch"] = host_arch
        (dst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    return True, f"promoted to {layout.show(dst)}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("instance", nargs="*")
    p.add_argument("--all", action="store_true", help="every valid instance in generated/")
    p.add_argument("--host-arch", choices=("x86_64", "aarch64"),
                   help="record the validated worker architecture in promoted metadata")
    args = p.parse_args()
    ids = (sorted(d.name for d in GENERATED.iterdir() if (d / "validation.json").exists())
           if args.all else args.instance)
    if not ids:
        p.error("give instance ids or --all")
    ok = 0
    for iid in ids:
        done, msg = promote(iid, host_arch=args.host_arch)
        ok += done
        print(f"  [{'ok  ' if done else 'skip'}] {iid}: {msg}")
    print(f"\n  {ok}/{len(ids)} promoted")


if __name__ == "__main__":
    main()
