#!/usr/bin/env python3
"""Genetic algorithm over NuttX apps-triage versions (Phase 1 tuning).

An individual is one frozen triage version: the base prompt
(triage/ga/base_prompt.md) plus an ordered set of named rules from
triage/ga/rules.json, and knobs (tool-call cap, minutes, sampling, check
strictness). Its sha covers the rendered prompt, the knobs and the code
snapshot it runs, so an unchanged individual keeps its sha and its repeats
accumulate. Individuals live in triage/ga/individuals/<sha>/ (spec.json and
the rendered prompt); Scholar holds a snapshot of the code per individual,
and results under /scratch/scholar/zhan5136/phase1-results/ga/gen<N>/<sha>/
rep<k>/{normal,blind}/.

    nuttx_ga.py new   <spec.json> ...      create individuals (guard, sha)
    nuttx_ga.py ship  <gen> <sha> ...      snapshot them on Scholar
    nuttx_ga.py submit <gen> <reps> <sha> ...   queue normal+blind runs
    nuttx_ga.py collect                    read scorecards, print the ranking
    nuttx_ga.py breed <gen> <n>            write the next generation's specs

Fitness is the mean normal score over repeats; ties go to the worst repeat,
then the mean blind score. An individual whose prompt or added code names a
gold case (PR numbers, commit hashes, file names) is rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GA = ROOT / "triage" / "ga"
INDIVIDUALS = GA / "individuals"
GOLD = ROOT / "triage" / "nuttx_apps_gold.json"
HOST = "scholar.rcac.purdue.edu"
REMOTE = "/scratch/scholar/zhan5136/ga"
RESULTS = "/scratch/scholar/zhan5136/phase1-results/ga"
PATH_FIX = 'export PATH=$(printf "%s" "$PATH" | tr ":" "\\n" | grep -v "^/apps/cent7" | paste -sd: -);'
CODE_PATHS = ["scripts", "templates", "harness", "docs", "triage/nuttx_apps_gold.json",
              "triage/models.json", "requirements.txt"]
# File names every NuttX change touches; naming them says nothing about a case.
GENERIC_FILES = {"Makefile", "CMakeLists.txt", "Kconfig", "ostest_main.c", "ostest.h",
                 "Make.defs", "defconfig", "README.md", "sched.h", "config.h"}
DEFAULT_KNOBS = {"max_tool_calls": 40, "max_minutes": 30, "parallel": 4,
                 "temperature": 1.0, "top_p": 0.95, "top_k": 20, "followup_review": False}


def ssh(command: str, stdin: str | None = None, timeout: int = 600) -> str:
    done = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30", HOST,
                           PATH_FIX + command], input=stdin, capture_output=True, text=True,
                          timeout=timeout)
    if done.returncode:
        raise RuntimeError(f"ssh failed ({done.returncode}): {done.stderr[-600:]}")
    return done.stdout


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          check=True).stdout


def load_rules() -> dict:
    return json.loads((GA / "rules.json").read_text())


def render_prompt(rule_names: list[str], rules: dict) -> str:
    """The base prompt with each rule's text added at the end of its section
    (1-5, the `## N.` headings), in the order given."""
    parts = re.split(r"(?m)^(?=## \d\. )", (GA / "base_prompt.md").read_text())
    for name in rule_names:
        rule = rules["rules"][name]
        index = rule["section"]
        parts[index] = parts[index].rstrip("\n") + "\n\n" + rule["text"].strip("\n") + "\n\n"
    return "".join(parts).rstrip("\n") + "\n"


def code_hash(commit: str = "HEAD") -> str:
    """Identifies the code an individual runs: the shipped files at a commit,
    except the prompt (the individual's own) and the GA machinery itself."""
    tree = git("ls-tree", "-r", commit, "--", *CODE_PATHS)
    kept = [line for line in tree.splitlines()
            if not re.search(r"scripts/(ga_|nuttx_ga)|docs/nuttx_apps_triage\.md", line)]
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()[:10]


def individual_sha(prompt: str, knobs: dict, code: str) -> str:
    blob = json.dumps([prompt, sorted(knobs.items()), code])
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


def gold_markers() -> dict:
    """What an individual must never name: the gold cases' PR numbers,
    commit hashes and file names."""
    gold = json.loads(GOLD.read_text())["cases"]
    numbers, hashes, files = set(), set(), set()
    sys.path.insert(0, str(ROOT / "scripts"))
    import nuttx_candidates as candidates
    for case, entry in gold.items():
        numbers.add(case.split("-")[1])
        text = json.dumps(entry)
        for item in entry.get("must_find", []) + entry.get("must_not_propose", []):
            numbers.add(str(item["kernel_pr"]))
            numbers.update(str(n) for n in item.get("also_needs", []))
        hashes.update(m.lower() for m in re.findall(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", text))
        try:
            record = candidates.apps_record(int(case.split("-")[1]))
        except Exception:    # no cached record: the numbers still guard
            continue
        files.update(Path(f["filename"]).name for f in record["apps"]["files"])
        for item in entry.get("must_find", []):
            with_kernel = candidates.kernel_record(item["kernel_pr"])
            files.update(Path(f["filename"]).name for f in with_kernel["files"])
    return {"numbers": numbers, "hashes": hashes, "files": files - GENERIC_FILES}


def guard(text: str, markers: dict | None = None) -> list[str]:
    """Reasons to reject added prompt or code text that names a gold case."""
    markers = markers or gold_markers()
    found = []
    for number in sorted(markers["numbers"], key=int):
        if re.search(rf"(?<![\w.]){number}(?![\w])", text):
            found.append(f"PR number {number}")
    lowered = text.lower()
    found += [f"commit hash {h}" for h in sorted(markers["hashes"])
              if h[:7] in lowered]
    found += [f"file name {name}" for name in sorted(markers["files"])
              if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w])", text)]
    return found


def added_code(base_commit: str) -> str:
    """Lines added to scripts/ since the GA's base commit."""
    diff = git("diff", base_commit, "HEAD", "--", "scripts", "templates", "harness")
    return "\n".join(line[1:] for line in diff.splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def new_individual(rule_names: list[str], knobs: dict, parents: list[str], gen: int,
                   markers: dict | None = None) -> tuple[str, list[str]]:
    """Create (or find) the individual; returns its sha and any guard
    violations (then nothing is written)."""
    rules = load_rules()
    knobs = {**DEFAULT_KNOBS, **knobs}
    prompt = render_prompt(rule_names, rules)
    base = json.loads((GA / "base.json").read_text())
    added = "\n".join(rules["rules"][n]["text"] for n in rule_names)
    problems = guard(added + "\n" + added_code(base["code_commit"]), markers)
    sha = individual_sha(prompt, knobs, code_hash())
    if problems:
        return sha, problems
    directory = INDIVIDUALS / sha
    if not directory.exists():
        directory.mkdir(parents=True)
        (directory / "nuttx_apps_triage.md").write_text(prompt)
        (directory / "spec.json").write_text(json.dumps(
            {"sha": sha, "gen": gen, "rules": rule_names, "knobs": knobs, "parents": parents,
             "code": code_hash(), "code_commit": git("rev-parse", "HEAD").strip()}, indent=1))
    return sha, []


def ship(sha: str) -> None:
    """Snapshot of the individual on Scholar: the committed code with its own
    prompt and sampling, as ga/snap/<sha>/."""
    spec = json.loads((INDIVIDUALS / sha / "spec.json").read_text())
    knobs = spec["knobs"]
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "code.tar"
        subprocess.run(["git", "-C", str(ROOT), "archive", spec["code_commit"], "-o", str(tar),
                        *CODE_PATHS], check=True)
        models = json.loads(subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{spec['code_commit']}:triage/models.json"],
            capture_output=True, text=True, check=True).stdout)
        models["qwen38-nvfp4"]["sampling"] = {
            **models["qwen38-nvfp4"]["sampling"], "temperature": knobs["temperature"],
            "top_p": knobs["top_p"], "top_k": knobs["top_k"]}
        stage = Path(tmp) / "stage"
        stage.mkdir()
        subprocess.run(["tar", "-xf", str(tar), "-C", str(stage)], check=True)
        (stage / "docs" / "nuttx_apps_triage.md").write_text(
            (INDIVIDUALS / sha / "nuttx_apps_triage.md").read_text())
        (stage / "triage" / "models.json").write_text(json.dumps(models, indent=2))
        (stage / "ga_individual.json").write_text((INDIVIDUALS / sha / "spec.json").read_text())
        subprocess.run(["tar", "-cf", str(Path(tmp) / "snap.tar"), "-C", str(stage), "."], check=True)
        remote = f"{REMOTE}/snap/{sha}"
        with open(Path(tmp) / "snap.tar", "rb") as handle:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", HOST,
                 f"rm -rf {remote} && mkdir -p {remote} && tar -xf - -C {remote}"],
                stdin=handle, capture_output=True)
        if done.returncode:
            raise RuntimeError(done.stderr.decode()[-500:])


def submit(gen: int, reps: int, shas: list[str], first_rep: dict[str, int] | None = None) -> int:
    """Queue, for every sha, `reps` repeats of a normal and a blind run.
    first_rep gives the next repeat number of a sha (elites continue)."""
    tasks, seq = [], 0
    for offset in range(reps):
        for mode in ("normal", "blind"):
            for sha in shas:
                rep = (first_rep or {}).get(sha, 1) + offset
                name = f"g{gen:02d}-{seq:04d}-{sha}-r{rep}-{mode}"
                seq += 1
                tasks.append({"name": name, "sha": sha, "rep": rep, "mode": mode, "gen": gen,
                              "snap": f"{REMOTE}/snap/{sha}",
                              "out": f"{RESULTS}/gen{gen:02d}/{sha}/rep{rep}/{mode}"})
    ssh("python3 -c 'import json,sys,os\n"
        "for t in json.load(sys.stdin):\n"
        f" p=\"{REMOTE}/queue/\"+t[\"name\"]+\".json\"; open(p+\".tmp\",\"w\").write(json.dumps(t)); os.rename(p+\".tmp\",p)'",
        stdin=json.dumps(tasks))
    return len(tasks)


COLLECT = r'''
import json, glob, os, re
out = {}
for path in glob.glob("%(results)s/gen*/*/rep*/*/scorecard.json"):
    m = re.search(r"/gen(\d+)/(\w+)/rep(\d+)/(normal|blind)/", path)
    s = json.load(open(path))
    out["%%s/%%s/%%s/%%s" %% m.groups()] = {
        "correct": s["correct"], "scored": s["scored"],
        "misses": [r["case"] for r in s["rows"] if not r["correct"]],
        "violations": s["violations"], "must_find": [s["must_find_found"], s["must_find_total"]],
        "exhausted": sum(bool(r["budget"].get("exhausted")) for r in s["rows"]),
        "errors": [r["case"] for r in s["rows"] if r["got_kind"] in ("error", "budget_exhausted", "missing")],
        "path": os.path.dirname(path)}
done = {os.path.basename(p)[:-5]: json.load(open(p)) for p in glob.glob("%(ga)s/done/*.json")}
print(json.dumps({"scores": out, "done": {k: {"exit": v.get("exit"), "eval_exit": v.get("eval_exit"),
      "seconds": v.get("seconds"), "node": v.get("node")} for k, v in done.items()},
      "queued": len(glob.glob("%(ga)s/queue/*.json")), "running": os.listdir("%(ga)s/running")}))
'''


def collect() -> dict:
    return json.loads(ssh("python3 -", stdin=COLLECT % {"results": RESULTS, "ga": REMOTE}))


def fitness(results: dict, sha: str) -> dict:
    """Per-repeat scores of an individual: {rep: [normal, blind]}."""
    reps: dict[str, dict] = {}
    for key, score in results["scores"].items():
        gen, owner, rep, mode = key.split("/")
        if owner == sha:
            reps.setdefault(f"{gen}/{rep}", {})[mode] = score
    complete = {k: v for k, v in reps.items() if "normal" in v}
    normals = [v["normal"]["correct"] for v in complete.values()]
    blinds = [v["blind"]["correct"] for v in complete.values() if "blind" in v]
    return {"sha": sha, "reps": complete, "normal": normals, "blind": blinds,
            "mean": sum(normals) / len(normals) if normals else 0.0,
            "worst": min(normals) if normals else 0,
            "mean_blind": sum(blinds) / len(blinds) if blinds else 0.0,
            "perfect": sum(v["normal"]["correct"] == 13 and v["normal"]["scored"] == 13
                           for v in complete.values())}


def ranking(results: dict) -> list[dict]:
    shas = sorted({key.split("/")[1] for key in results["scores"]})
    return sorted((fitness(results, sha) for sha in shas),
                  key=lambda f: (-f["mean"], -f["worst"], -f["mean_blind"]))


def breed(parents: list[dict], count: int, gen: int, rng: random.Random,
          markers: dict | None = None, avoid: set[str] = frozenset()) -> list[str]:
    """Crossover of two parents' rules and knobs, then a mutation or two;
    returns the new individuals' shas."""
    rules = load_rules()
    space = rules["knob_space"]
    specs = [json.loads((INDIVIDUALS / p["sha"] / "spec.json").read_text()) for p in parents]
    made, seen, attempts = [], set(avoid), 0
    while len(made) < count and attempts < count * 40:
        attempts += 1
        a, b = rng.sample(specs, 2) if len(specs) > 1 else (specs[0], specs[0])
        chosen = [r for r in dict.fromkeys(a["rules"] + b["rules"])
                  if r in a["rules"] and r in b["rules"] or rng.random() < 0.5]
        knobs = {k: rng.choice([a["knobs"][k], b["knobs"][k]]) for k in a["knobs"]}
        for _ in range(rng.choice([1, 1, 2])):
            move = rng.choice(["add", "add", "drop", "knob"])
            if move == "add":
                pool = [r for r in rules["rules"] if r not in chosen]
                if pool:
                    chosen.append(rng.choice(pool))
            elif move == "drop" and chosen:
                chosen.remove(rng.choice(chosen))
            else:
                key = rng.choice(list(space))
                knobs[key] = rng.choice(space[key])
        for group in rules.get("exclusive", []):   # at most one rule of a group
            present = [r for r in chosen if r in group]
            for extra in present[:-1]:
                chosen.remove(extra)
        sha, problems = new_individual(chosen, knobs, [a["sha"], b["sha"]], gen, markers)
        if problems or sha in seen:
            continue
        seen.add(sha)
        made.append(sha)
    return made


def write_status(gen: int, results: dict, next_text: str) -> Path:
    """Overwrite triage/PHASE1_STATUS.md for the end of a generation."""
    ranked = [f for f in ranking(results) if f["normal"]]
    pools = ssh("squeue -u zhan5136 -h -o '%i %P %D nodes, ends %e'").strip()
    best = max((f["perfect"] for f in ranked), default=0)
    lines = [
        "# NuttX Phase 1 tuning status", "",
        f"Generation {gen} complete (updated {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}).",
        "", "- Bar: one unchanged individual with 2 repeats at 13/13 on the normal gold; blind runs",
        "  (9 cases with a must_find) are tracked only.",
        f"- Best count: {best} perfect normal repeats of one individual (need 2).",
        f"- Individuals scored so far: {len(ranked)}; runs scored: {len(results['scores'])}.",
        "- Fitness: mean normal score over repeats; ties by worst repeat, then mean blind.",
        "", "Pool jobs (servers on every node; workers run as srun steps):", "", "```", pools, "```",
        "", "## Top 5", "",
        "| sha | rules | knobs changed | normal per repeat | blind per repeat | mean | worst |",
        "|---|---|---|---|---|---|---|"]
    for f in ranked[:5]:
        spec = json.loads((INDIVIDUALS / f["sha"] / "spec.json").read_text())
        changed = {k: v for k, v in spec["knobs"].items() if v != DEFAULT_KNOBS.get(k)}
        lines.append(f"| `{f['sha']}` | {', '.join(spec['rules']) or '(base prompt)'} | "
                     f"{changed or '-'} | {f['normal']} | {f['blind']} | {f['mean']:.2f} | {f['worst']} |")
    lines += ["", "## Next experiments", "", next_text.strip(), ""]
    path = ROOT / "triage" / "PHASE1_STATUS.md"
    path.write_text("\n".join(lines))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("gen", type=int)
    p.add_argument("specs", nargs="+", type=Path, help='JSON {"rules": [...], "knobs": {...}}')
    p = sub.add_parser("ship")
    p.add_argument("shas", nargs="+")
    p = sub.add_parser("submit")
    p.add_argument("gen", type=int)
    p.add_argument("reps", type=int)
    p.add_argument("shas", nargs="+")
    p.add_argument("--first-rep", type=int, default=1)
    sub.add_parser("collect")
    p = sub.add_parser("status")
    p.add_argument("gen", type=int)
    p.add_argument("--next", default="")
    args = parser.parse_args()
    if args.cmd == "new":
        markers = gold_markers()
        for path in args.specs:
            for spec in json.loads(path.read_text()):
                sha, problems = new_individual(spec["rules"], spec.get("knobs", {}),
                                               spec.get("parents", []), args.gen, markers)
                print(sha, "REJECTED: " + "; ".join(problems) if problems else "ok")
    elif args.cmd == "ship":
        for sha in args.shas:
            ship(sha)
            print("shipped", sha)
    elif args.cmd == "submit":
        print(submit(args.gen, args.reps, args.shas,
                     {sha: args.first_rep for sha in args.shas}), "tasks queued")
    elif args.cmd == "status":
        print(write_status(args.gen, collect(), args.next))
    else:
        for f in ranking(collect()):
            print(f["sha"], f"normal {f['normal']} blind {f['blind']} mean {f['mean']:.2f}")


if __name__ == "__main__":
    main()
