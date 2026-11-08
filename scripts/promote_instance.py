#!/usr/bin/env python3
"""
Promote validated Zephyr instances from generated/ to apptainer/instances/.

    python scripts/promote_instance.py zephyr__zephyr-65697
    python scripts/promote_instance.py --all            # every valid one in generated/

apptainer/instances/ is the committed Zephyr benchmark: the instances the
harness runs agents on. Only an instance that passed validation gets there,
and only as validated: its recipe files must still match the fingerprint
validate_instance.py recorded in validation.json. A recipe regenerated or
edited after validation is refused until it is validated again.

Copies the recipe and its record -- instance.def, instance.json,
run_tests.py, test_patch.diff (Contiki-NG: and bench.json), metadata.json,
validation.json. Images are not in git; build_instances.py rebuilds them from
the recipe. Contiki-NG instances go to apptainer/instances/contiki-ng/ and also
need a clean leak check, five identical before runs and a checked statement
(contiki_problem).
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

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED = layout.generated()
DEST = REPO_ROOT / "apptainer" / "instances"
RECORD_FILES = ("metadata.json", "validation.json")
# Kept in step with triage_prs.STATEMENT_CHECK (and cluster/status.py).
STATEMENT_CHECK = 2
DETERMINISM_RUNS = 5


def contiki_problem(report: dict, meta: dict) -> str | None:
    """Why a Contiki-NG instance may not be promoted, or None: it needs a clean
    leak check under an isolated network, five identical before runs, and a
    statement that passed the current statement check and the leak check's
    fix-identifier check (at validation, or by the statement phase since)."""
    leak = report.get("leak_check")
    if not leak:
        return "no leak check record; validate it again"
    if not leak.get("ok") or leak.get("network") != "isolated":
        return f"leak check failed: {leak.get('findings')}"
    d = report.get("determinism") or {}
    if d.get("runs", 0) < DETERMINISM_RUNS or not d.get("ok"):
        return f"no deterministic before state ({DETERMINISM_RUNS} runs); validate it again"
    check = meta.get("statement_check") or {}
    if check.get("version") != STATEMENT_CHECK or check.get("result") not in ("kept", "written"):
        return "the statement has not passed the current statement check"
    sha = hashlib.sha256((meta.get("problem_statement") or "").encode()).hexdigest()
    later = meta.get("statement_leak_check") or {}
    if leak.get("statement_sha256") != sha and not (later.get("statement_sha256") == sha
                                                    and later.get("fix_identifiers") == []):
        return "the statement changed since its leak check; recheck the statement or validate again"
    return None


def promote(instance_id: str, source: Path = GENERATED) -> tuple[bool, str]:
    src = source / instance_id
    report_path = src / "validation.json"
    if not report_path.exists():
        return False, "not validated (no validation.json)"
    report = json.loads(report_path.read_text())
    if not report.get("valid"):
        return False, f"invalid: {report.get('reason')}"
    want = report.get("recipe_sha256")
    if not want:
        return False, "validation.json predates recipe fingerprints; validate it again"
    if au.recipe_fingerprint(src) != want:
        return False, "recipe changed since it was validated; validate it again"
    meta = json.loads((src / "metadata.json").read_text())
    if not (meta.get("problem_statement") or "").strip():
        return False, "no problem statement (metadata.json problem_statement is empty)"
    contiki = meta.get("project") == "contiki-ng"
    if contiki:
        why = contiki_problem(report, meta)
        if why:
            return False, why
    files = (*au.recipe_files(src), *RECORD_FILES)
    missing = [f for f in files if not (src / f).exists()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    dst = (DEST / "contiki-ng" if contiki else DEST) / instance_id
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(src / f, dst / f)
    return True, f"promoted to {layout.show(dst)}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("instance", nargs="*")
    p.add_argument("--all", action="store_true", help="every valid instance in generated/")
    p.add_argument("--source", type=Path, default=GENERATED,
                   help="folder holding the instances (default: generated/; a Contiki "
                        "observation run's are in <run>/finalized/)")
    args = p.parse_args()
    ids = (sorted(d.name for d in args.source.iterdir() if (d / "validation.json").exists())
           if args.all else args.instance)
    if not ids:
        p.error("give instance ids or --all")
    ok = 0
    for iid in ids:
        done, msg = promote(iid, args.source)
        ok += done
        print(f"  [{'ok  ' if done else 'skip'}] {iid}: {msg}")
    print(f"\n  {ok}/{len(ids)} promoted")


if __name__ == "__main__":
    main()
