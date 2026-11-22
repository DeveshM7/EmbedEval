#!/usr/bin/env python3
"""
Draw the evaluation sample of one project: a stratified random sample, the same
for every model.

    .venv/bin/python scripts/sample_problems.py zephyr 50
    .venv/bin/python scripts/sample_problems.py riot 43 --exclude riot__riot-17188 riot__riot-19400

Eligible problems: published and valid (validation.json), with a verified
statement ("fine" or "fixed"), minus --exclude (the pilot problems by default),
and one problem per duplicate group: the same fix merged more than once (Zephyr
backports to release branches) is one problem. Duplicates share the fix diff's
`git patch-id`, or the fix commit's subject, test and files; the lowest PR
number (the original) is kept.

Strata: kind (fix / feature) x area. A Zephyr problem's area is the folder under
tests/ of its test (net, subsys, lib, drivers, kernel, ...); elsewhere, the top
folder most of its fix's files are in (include/ counts as the folder below it).
Areas with fewer than --min-area problems are merged into "other".

Allocation: in proportion to each stratum's size (largest remainder), with at
least --min-per-stratum from every stratum that has that many; the extra picks
come from the largest strata. Within a stratum, problems are drawn at random.

Check: the sample's spread over files changed (1 / 2-3 / 4+) and change size
(lines added and removed by the fix in its own files: small / medium / large,
by the population's terciles) is compared with the population's. If any bucket's
share is more than --max-gap points (10) off, the draw is redone with the next seed;
every attempt is recorded.

Writes eval-sets/<project>.txt (one id per line) and eval-sets/<project>.json
(settings, seeds tried, each problem's measures, population vs sample spread).
Same inputs, same sample.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import instances  # noqa: E402

PILOTS = ["zephyr__zephyr-100085", "zephyr__zephyr-102462", "zephyr__zephyr-104156",
          "riot__riot-17188", "riot__riot-19400"]
# Bare clones of each project's repository, for the size of each fix.
CLONES = {"zephyr": "/scratch/scholar/{user}/embedeval/shared/zephyr.git",
          "riot": "/scratch/scholar/{user}/embedeval/shared/riot/riot.git",
          "nuttx": "/scratch/scholar/{user}/embedeval/shared/nuttx.git",
          "mynewt": "/scratch/scholar/{user}/embedeval/shared/mynewt-core.git",
          "contiki-ng": "/scratch/scholar/{user}/embedeval/shared/contiki-ng.git"}


def as_list(v) -> list:
    """Some metadata lists are stored as their Python text ("['a', 'b']")."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        import ast
        return list(ast.literal_eval(v))
    return [v] if v else []


def area_of(meta: dict) -> str:
    tp = meta.get("test_path") or ""
    if meta.get("project") == "zephyr" and tp.startswith("tests/") and tp.count("/") >= 1:
        return tp.split("/")[1]
    tops = []
    for f in as_list(meta.get("files_changed_by_fix")):
        parts = f.split("/")
        tops.append(parts[1] if parts[0] == "include" and len(parts) > 2 else parts[0])
    return collections.Counter(tops).most_common(1)[0][0] if tops else "unknown"


def fix_size(clone: str, meta: dict) -> int:
    """Lines added + removed by the fix, in the fix's own files."""
    files = as_list(meta.get("files_changed_by_fix"))
    out = subprocess.run(["git", "-C", clone, "diff", "--numstat", meta["base_commit"],
                          meta["fix_commit"], "--", *files],
                         capture_output=True, text=True, check=True).stdout
    n = 0
    for line in out.splitlines():
        a, d, _ = line.split("\t", 2)
        n += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return n


def dedup_keys(clone: str, meta: dict) -> list:
    """Two keys; problems sharing either are duplicates."""
    files = as_list(meta.get("files_changed_by_fix"))
    diff = subprocess.run(["git", "-C", clone, "diff", meta["base_commit"], meta["fix_commit"], "--", *files],
                          capture_output=True, check=True).stdout
    pid = subprocess.run(["git", "-C", clone, "patch-id", "--stable"], input=diff,
                         capture_output=True, check=True).stdout.split()[:1]
    subject = subprocess.run(["git", "-C", clone, "log", "-1", "--format=%s", meta["fix_commit"]],
                             capture_output=True, text=True, check=True).stdout.strip()
    keys = [("subject", subject, meta.get("test_path", ""), tuple(sorted(files)))]
    if pid:
        keys.append(("patch-id", pid[0].decode()))
    return keys


def pr_number(iid: str) -> int:
    return int(iid.rsplit("-", 1)[1])


def drop_duplicates(rows: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Keep the lowest PR of each group of problems linked by a shared key."""
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    first: dict = {}
    for r in rows:
        for k in r.pop("keys"):
            if k in first:
                a, b = find(first[k]), find(r["id"])
                if a != b:
                    lo, hi = sorted((a, b), key=pr_number)
                    parent[hi] = lo
            else:
                first[k] = r["id"]
    dups = {r["id"]: find(r["id"]) for r in rows if find(r["id"]) != r["id"]}
    return [r for r in rows if r["id"] not in dups], dups


def files_bucket(n: int) -> str:
    return "1" if n == 1 else "2-3" if n <= 3 else "4+"


def eligible(project: str, exclude: set[str]) -> tuple[list[dict], dict, dict[str, str]]:
    import getpass
    clone = CLONES[project].format(user=getpass.getuser())
    rows, dropped = [], collections.Counter()
    for iid in instances.discover_instances(project):
        if iid in exclude:
            dropped["excluded"] += 1
            continue
        d = instances.instance_dir(iid)
        meta = json.loads((d / "metadata.json").read_text())
        val = json.loads((d / "validation.json").read_text()) if (d / "validation.json").exists() else {}
        if not (val.get("valid") and val.get("published")):
            dropped["not valid and published"] += 1
            continue
        if (meta.get("verification") or {}).get("status") not in ("fine", "fixed"):
            dropped["statement not verified"] += 1
            continue
        files = as_list(meta.get("files_changed_by_fix"))
        rows.append({"id": iid, "kind": meta.get("change_type") or "unknown", "area": area_of(meta),
                     "files": len(files), "size": fix_size(clone, meta), "keys": dedup_keys(clone, meta)})
    rows, dups = drop_duplicates(sorted(rows, key=lambda r: pr_number(r["id"])))
    if dups:
        dropped["duplicate"] = len(dups)
    return rows, dict(dropped), dups


def terciles(values: list[int]) -> tuple[int, int]:
    v = sorted(values)
    return v[len(v) // 3], v[2 * len(v) // 3]


def allocate(sizes: dict[str, int], n: int, minimum: int) -> dict[str, int]:
    total = sum(sizes.values())
    quota = {k: n * s / total for k, s in sizes.items()}
    alloc = {k: int(q) for k, q in quota.items()}
    for k in sorted(quota, key=lambda k: (-(quota[k] - alloc[k]), k))[:n - sum(alloc.values())]:
        alloc[k] += 1
    for k, s in sizes.items():                      # at least `minimum` from each stratum that has them
        alloc[k] = max(alloc[k], min(minimum, s))
    while sum(alloc.values()) > n:                  # pay for the extras from the largest strata
        k = max((k for k in alloc if alloc[k] > min(minimum, sizes[k])),
                key=lambda k: (alloc[k] - quota[k], alloc[k], k))
        alloc[k] -= 1
    return alloc


def spread(rows: list[dict], key) -> dict[str, float]:
    c = collections.Counter(key(r) for r in rows)
    return {k: round(100 * v / len(rows), 1) for k, v in sorted(c.items())}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project", choices=sorted(CLONES))
    p.add_argument("n", type=int)
    p.add_argument("--seed", type=int, default=20261116)
    p.add_argument("--exclude", nargs="*", default=PILOTS)
    p.add_argument("--min-area", type=int, default=10)
    p.add_argument("--min-per-stratum", type=int, default=2)
    p.add_argument("--max-gap", type=float, default=10.0, help="percentage points")
    p.add_argument("--max-tries", type=int, default=50)
    p.add_argument("--out", default=str(REPO / "eval-sets"))
    a = p.parse_args()

    rows, dropped, dups = eligible(a.project, set(a.exclude))
    if a.n > len(rows):
        sys.exit(f"{a.n} asked, {len(rows)} eligible")
    big = {k for k, v in collections.Counter(r["area"] for r in rows).items() if v >= a.min_area}
    lo, hi = terciles([r["size"] for r in rows])
    for r in rows:
        r["area_group"] = r["area"] if r["area"] in big else "other"
        r["stratum"] = f"{r['kind']}/{r['area_group']}"
        r["files_bucket"] = files_bucket(r["files"])
        r["size_bucket"] = "small" if r["size"] <= lo else "medium" if r["size"] <= hi else "large"
    strata = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: r["id"]):
        strata[r["stratum"]].append(r)
    alloc = allocate({k: len(v) for k, v in strata.items()}, a.n, a.min_per_stratum)

    measures = {"kind": lambda r: r["kind"], "area": lambda r: r["area_group"],
                "files": lambda r: r["files_bucket"], "size": lambda r: r["size_bucket"]}
    population = {m: spread(rows, f) for m, f in measures.items()}
    tries = []
    for seed in range(a.seed, a.seed + a.max_tries):
        rng = random.Random(seed)
        sample = [r for k in sorted(strata) for r in rng.sample(strata[k], alloc[k])]
        got = {m: spread(sample, f) for m, f in measures.items()}
        gap = max(abs(got[m].get(b, 0) - population[m][b]) for m in ("files", "size") for b in population[m])
        tries.append({"seed": seed, "largest_gap": round(gap, 1)})
        if gap <= a.max_gap:
            break
    else:
        sys.exit(f"no draw within {a.max_gap} points in {a.max_tries} seeds: {tries[-3:]}")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = sorted(r["id"] for r in sample)
    (out / f"{a.project}.txt").write_text("\n".join(ids) + "\n")
    record = {
        "project": a.project, "n": a.n, "seed_used": seed, "seeds_tried": tries,
        "settings": {"first_seed": a.seed, "exclude": sorted(a.exclude), "min_area": a.min_area,
                     "min_per_stratum": a.min_per_stratum, "max_gap": a.max_gap,
                     "size_terciles": [lo, hi]},
        "eligible": len(rows), "dropped": dropped, "duplicates": dict(sorted(dups.items())),
        "allocation": {k: {"population": len(strata[k]), "sample": alloc[k]} for k in sorted(strata)},
        "spread": {m: {"population": population[m], "sample": got[m]} for m in measures},
        "sample": [{k: r[k] for k in ("id", "kind", "area", "area_group", "files", "size", "size_bucket")}
                   for r in sorted(sample, key=lambda r: r["id"])],
    }
    (out / f"{a.project}.json").write_text(json.dumps(record, indent=2) + "\n")

    print(f"{a.project}: {len(rows)} eligible (dropped {dropped}), sample {a.n}, seed {seed} "
          f"({len(tries)} tried), size terciles <= {lo} / <= {hi} lines")
    print(f"{'stratum':28s} population sample")
    for k in sorted(strata):
        print(f"  {k:26s} {len(strata[k]):5d} {alloc[k]:6d}")
    for m in measures:
        print(f"{m:6s} population {population[m]}\n       sample     {got[m]}")
    print(f"wrote {out / (a.project + '.txt')} and .json")


if __name__ == "__main__":
    main()
