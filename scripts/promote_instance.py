#!/usr/bin/env python3
"""
Promote validated Zephyr and NuttX instances from generated/ to apptainer/instances/
(NuttX: apptainer/instances/nuttx/).

    python scripts/promote_instance.py zephyr__zephyr-65697
    python scripts/promote_instance.py --all            # every valid one in generated/
    python scripts/promote_instance.py --host-arch x86_64 nuttx__nuttx-14152-a2725

apptainer/instances/ is the committed Apptainer benchmark: the instances the
harness runs agents on. Only an instance that passed validation gets there,
and only as validated: its recipe files must still match the fingerprint
validate_instance.py recorded in validation.json. A recipe regenerated or
edited after validation is refused until it is validated again.

--host-arch records in the promoted metadata that the instance must build,
validate and run on that architecture, for one found to validate on it only.

A NuttX instance also needs, in its validation.json, a clean leak check with
the network isolated (scripts/nuttx_leakcheck.py) and a before state judged
the same in 5 runs (validate_instance.py --determinism 5), and in its
metadata a problem statement that passed the current statement check
(scripts/nuttx_statement.py). Its host_arch is the architecture it validated
on, unless --host-arch says otherwise.

Copies the recipe and its record -- instance.def, instance.json,
run_tests.py, test_patch.diff (NuttX: instance.def, run_tests.sh,
test_patch.diff), metadata.json, validation.json. Images are not in git;
build_instances.py rebuilds them from the recipe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402  (first: reads --run-dir)
import apptainer_util as au  # noqa: E402
import instances  # noqa: E402
import nuttx_statement  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED = layout.generated()
DEST = REPO_ROOT / "apptainer" / "instances"
RECORD_FILES = ("metadata.json", "validation.json")


def promote(instance_id: str, source: Path = GENERATED,
            host_arch: str | None = None) -> tuple[bool, str]:
    src = source / instance_id
    nuttx = instances.split_instance(instance_id)[0] == "nuttx"
    recipe = au.nuttx_recipe_files(src) if nuttx else au.RECIPE_FILES
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
    if nuttx:
        why = nuttx_gate(report, json.loads((src / "metadata.json").read_text()))
        if why:
            return False, why
        host_arch = host_arch or report.get("host")
    files = (*recipe, *RECORD_FILES)
    missing = [f for f in files if not (src / f).exists()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    dst = (DEST / "nuttx" if nuttx else DEST) / instance_id
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(src / f, dst / f)
    if host_arch:
        meta = json.loads((dst / "metadata.json").read_text())
        meta["host_arch"] = host_arch
        (dst / "metadata.json").write_text(json.dumps(meta, indent=4) + "\n")
    return True, f"promoted to {layout.show(dst)}"


def nuttx_gate(report: dict, meta: dict) -> str | None:
    """Why a valid NuttX instance cannot be promoted, or None."""
    leak = report.get("leak_check")
    if not leak:
        return "no leak check record; validate it again"
    if not leak.get("ok") or leak.get("network") != "isolated":
        return f"leak check failed: {leak.get('findings')}"
    if "statement_ok" not in leak:
        return "the leak check predates the statement record; validate it again"
    if not leak["statement_ok"]:
        return f"the problem statement names the fix: {leak.get('statement_fix_identifiers')}"
    if leak.get("statement_sha256") != hashlib.sha256(
            (meta.get("problem_statement") or "").strip().encode()).hexdigest():
        return "the problem statement changed after its leak check; validate it again"
    d = report.get("determinism")
    if not d or d.get("runs", 0) < 5 or not d.get("ok"):
        return "no deterministic before state (validate with --determinism 5)"
    check = meta.get("statement_check") or {}
    if (check.get("version") != nuttx_statement.STATEMENT_CHECK or check.get("result") not in ("kept", "written")
            or not (meta.get("problem_statement") or "").strip()):
        return "no problem statement that passes the current statement check"
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("instance", nargs="*")
    p.add_argument("--all", action="store_true", help="every valid instance in generated/")
    p.add_argument("--host-arch", choices=("x86_64", "aarch64"),
                   help="the architecture the instances validate on only")
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
