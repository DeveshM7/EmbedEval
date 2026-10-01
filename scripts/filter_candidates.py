#!/usr/bin/env python3
"""
Hard filters for Zephyr PR candidates.

    python scripts/filter_candidates.py selftest
    python scripts/filter_candidates.py fetch --since 2024-01-01 --until 2024-06-30
    python scripts/filter_candidates.py filter
    python scripts/filter_candidates.py enrich

Three stages, deliberately separate:

    fetch    cheap, wide   -- title, body, labels, file list. Everything the
                             hard filters need and nothing more.
    filter   free, instant -- cuts the pool to PRs that could become instances.
    enrich   costly, narrow-- full GitHub context for the survivors only:
                             diffs, review threads, linked issues, commits,
                             the complete test suite each PR touches, the
                             pre-change source it modifies, and the board
                             files of our platforms at its base commit.

Enrich runs last because only ~15% of PRs survive filtering. Pulling the
expensive context for the whole pool would be about six times the API calls
for data that is thrown away. The hard filters use none of it.

Deliberately permissive. These filters exist to cut the pool down to PRs that
*could* become an instance, not to judge whether one should. Judgement is the
model's job in the triage stage -- anything rejected here is rejected forever
and never reaches it, so the bar is set as low as it can usefully go.

That is not conservatism for its own sake. Every stricter filter that looked
obvious was measured against the eight Zephyr instances already validated by
hand, and most of them threw good instances away:

    "adds a new test file"   rejects 5 of 8  (most fixes add cases to an
                                              existing test file)
    has the `bug` label      rejects 4 of 8
    <= 10 files changed      rejects 74435, which has 21
    has a linked issue       rejects 43405

So the rule is: a filter belongs here only if it cannot reject any of the
eight. `selftest` enforces exactly that -- run it after any change.

Requires the `gh` CLI, authenticated. Read-only.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = "zephyrproject-rtos/zephyr"
REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE = REPO_ROOT / "candidates" / "cache"
OUT = REPO_ROOT / "candidates" / "candidates.jsonl"
ENRICHED = REPO_ROOT / "candidates" / "enriched"

# Paths that are never the fix itself. A PR touching only these has nothing
# for an agent to repair.
NON_SOURCE_PREFIXES = ("tests/", "doc/", ".github/", "samples/")

# The eight instances already validated by hand. selftest asserts every one
# survives the filters; if one does not, the filter is wrong, not the PR.
KNOWN_GOOD = [33690, 43405, 62109, 65697, 74435, 82272, 85079, 89534]


# ---------------------------------------------------------------- github


def gh(path: str, retries: int = 3):
    """GET a GitHub API path via the gh CLI. Returns parsed JSON or None."""
    for attempt in range(retries):
        proc = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        err = proc.stderr.lower()
        if "rate limit" in err or "was submitted too quickly" in err:
            wait = 20 * (attempt + 1)
            print(f"  rate limited, sleeping {wait}s ...", file=sys.stderr)
            time.sleep(wait)
            continue
        return None
    return None


def fetch_pr(number: int) -> dict | None:
    """Fetch one PR plus its file list, and cache it."""
    cached = CACHE / f"{number}.json"
    if cached.exists():
        return json.loads(cached.read_text())

    pr = gh(f"repos/{REPO}/pulls/{number}")
    if not pr:
        return None

    # Paginate. 100 is the maximum page size this endpoint allows, so a single
    # request silently returns only the first 100 files of a larger PR. GitHub
    # stops serving files past 3000; that is a real ceiling, unlike page size.
    files, page = [], 1
    while True:
        batch = gh(f"repos/{REPO}/pulls/{number}/files?per_page=100&page={page}")
        if not batch:
            break
        files += batch
        if len(batch) < 100 or len(files) >= 3000:
            break
        page += 1

    record = {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "labels": [l["name"] for l in pr.get("labels", [])],
        "files": [
            {"filename": f["filename"], "status": f["status"],
             "additions": f["additions"], "deletions": f["deletions"]}
            for f in files
        ],
        # Only true at GitHub's hard 3000-file ceiling, where the list really
        # is incomplete and nothing we do can complete it.
        "files_truncated": len(files) >= 3000,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(record, indent=2))
    return record


def search_merged_prs(since: str, until: str) -> list[int]:
    """PR numbers merged in a date window. Windows must stay under 1000
    results -- that is a hard cap in GitHub's search API, and exceeding it
    silently truncates rather than erroring."""
    numbers, page = [], 1
    q = f"repo:{REPO}+is:pr+is:merged+merged:{since}..{until}"
    while True:
        data = gh(f"search/issues?q={q}&per_page=100&page={page}")
        if not data or not data.get("items"):
            break
        numbers += [i["number"] for i in data["items"]]
        if len(data["items"]) < 100 or page >= 10:
            if data.get("total_count", 0) > 1000:
                print(f"  WARNING: {data['total_count']} results in "
                      f"{since}..{until}; search caps at 1000. Narrow the "
                      f"window or candidates are being silently dropped.",
                      file=sys.stderr)
            break
        page += 1
    return numbers


# ---------------------------------------------------------------- filters


def classify(pr: dict) -> tuple[bool, str]:
    """Apply the hard filters. Returns (kept, reason)."""
    names = [f["filename"] for f in pr["files"]]

    if not pr.get("merged_at"):
        return False, "not merged"

    if pr["title"].lower().startswith("revert"):
        return False, "revert"

    if pr.get("files_truncated"):
        # Past GitHub's 3000-file ceiling the list genuinely cannot be
        # completed, so any judgement built on it would be unsound. This is a
        # limit of the data, not an opinion about PR size -- size alone is
        # never a reason to reject.
        return False, "file list incomplete (GitHub 3000-file ceiling)"

    tests = [n for n in names if n.startswith("tests/")]
    if not tests:
        return False, "touches no tests/"

    source = [n for n in names if not n.startswith(NON_SOURCE_PREFIXES)]
    if not source:
        return False, "touches no source outside tests/"

    return True, "ok"


def summarise(pr: dict) -> dict:
    """The record handed to the triage stage."""
    names = [f["filename"] for f in pr["files"]]
    tests = [n for n in names if n.startswith("tests/")]
    source = [n for n in names if not n.startswith(NON_SOURCE_PREFIXES)]
    return {
        "number": pr["number"],
        "title": pr["title"],
        "url": f"https://github.com/{REPO}/pull/{pr['number']}",
        "merged_at": pr["merged_at"],
        "merge_commit_sha": pr["merge_commit_sha"],
        "labels": pr["labels"],
        "n_files": len(names),
        "source_files": source,
        "test_files": tests,
        "test_dirs": sorted({"/".join(t.split("/")[:3]) for t in tests}),
    }



# ---------------------------------------------------------------- enrichment


ISSUE_REF = re.compile(r"(?i)\b(?:fixes|closes|resolves)\s+#(\d+)")


def gh_paged(path: str, cap: int = 1000) -> list:
    """Collect every page of a list endpoint."""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while len(out) < cap:
        batch = gh(f"{path}{sep}per_page=100&page={page}")
        if not batch:
            break
        out += batch
        if len(batch) < 100:
            break
        page += 1
    return out


# Zephyr marks a test suite by putting one of these in the directory. Taken
# from twister itself -- twisterlib/testplan.py walks every directory and
# treats any that contains one of these as a suite:
#
#     TEST_DEFINITION_FILENAME = ['testcase.yaml', 'tests.yaml', 'sample.yaml']
#
# `west build` uses a different marker (CMakeLists.txt), but the two land on
# the same directory because a suite has both.
SUITE_MARKERS = ("testcase.yaml", "tests.yaml", "sample.yaml")


def find_suite_root(file_path: str, sha: str, cache: dict) -> tuple[str | None, str | None]:
    """Walk up from a changed test file to the directory that owns it.

    tests/posix/common/src/key.c -> tests/posix/common

    The depth varies -- tests live in src/ under some suites and at the root
    of others -- so this cannot be a fixed number of path segments.
    """
    parts = file_path.split("/")
    for depth in range(len(parts) - 1, 1, -1):     # deepest first, stop above tests/
        d = "/".join(parts[:depth])
        if d not in cache:
            listing = gh(f"repos/{REPO}/contents/{d}?ref={sha}")
            names = {f["name"] for f in listing} if isinstance(listing, list) else set()
            cache[d] = next((m for m in SUITE_MARKERS if m in names), None)
        if cache[d]:
            return d, cache[d]
    return None, None


def fetch_suite(root: str, sha: str) -> dict:
    """Every file in a test suite directory, with contents.

    No size cap. Most Zephyr suites are four files; the occasional large
    shared suite is a problem for later, not now.
    """
    tree = gh(f"repos/{REPO}/git/trees/{sha}:{root}?recursive=1")
    if not tree:
        return {}
    out = {}
    for b in tree.get("tree", []):
        if b.get("type") != "blob":
            continue
        blob = gh(f"repos/{REPO}/git/blobs/{b['sha']}")
        content = None
        if blob and blob.get("encoding") == "base64":
            try:
                content = base64.b64decode(blob["content"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                content = None          # binary; recorded by name and size only
        out[b["path"]] = {"size": b.get("size"), "content": content}
    return out


def fetch_file(path: str, sha: str) -> dict:
    """One file at one commit: {size, content}. content is None for binary
    files and for files over the contents API's 1 MB limit."""
    meta = gh(f"repos/{REPO}/contents/{path}?ref={sha}")
    if not isinstance(meta, dict) or meta.get("type") != "file":
        return {"size": None, "content": None, "omitted": "not found at this commit"}
    content = None
    if meta.get("encoding") == "base64" and meta.get("content"):
        try:
            content = base64.b64decode(meta["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            content = None
    return {"size": meta.get("size"), "content": content}


# Budget for the pre-change source files, in characters: roughly 30K tokens.
# Records are otherwise at most ~41K tokens, so this keeps the largest prompt
# well inside the model's context while covering all but the largest fixes.
BASE_SOURCE_BUDGET = 120_000


def fetch_base_sources(files: list[dict], base: str) -> dict:
    """The version before the PR of every non-test file it modifies, removes
    or renames -- the code the fix is applied to.

    The diff alone shows changed lines with three lines of context. Judging
    whether a test detects the change often needs the function around it,
    and without this the triage model goes and reads it piecemeal through
    tools, one expensive round trip per chunk. Added files are skipped: they
    have no before-state, and the diff already shows them whole.

    Most-changed files first, until BASE_SOURCE_BUDGET is spent; any file
    that does not fit is still listed, with the reason it was left out.
    """
    wanted = [f for f in files
              if not f["filename"].startswith(NON_SOURCE_PREFIXES)
              and f["status"] != "added"]
    wanted.sort(key=lambda f: -(f["additions"] + f["deletions"]))
    out, spent = {}, 0
    for f in wanted:
        path = f.get("previous_filename") or f["filename"]
        got = fetch_file(path, base)
        if got["content"] is None:
            got.setdefault("omitted", "binary, or over the 1 MB API limit")
        elif spent + len(got["content"]) > BASE_SOURCE_BUDGET:
            got = {"size": got["size"], "content": None,
                   "omitted": f"over the {BASE_SOURCE_BUDGET:,}-character budget "
                              f"for pre-change sources; read it with a tool if needed"}
        else:
            spent += len(got["content"])
        out[path] = got
    return out


# The boards we can run, as pr_triage.md names them, and the board file each
# is defined by. Board folders moved over the years (boards/x86/qemu_x86/ ->
# boards/qemu/x86/), so the file is found by name anywhere under boards/.
OUR_PLATFORMS = {
    "native_sim": "native_sim.yaml",
    "native_sim/native/64": "native_sim_native_64.yaml",
    "native_sim_64": "native_sim_64.yaml",
    "qemu_x86": "qemu_x86.yaml",
}


def fetch_platforms(base: str) -> dict:
    """Which of our platforms exist at the PR's base commit, and what each
    declares it supports.

    A suite that `depends_on` a capability the board does not declare never
    runs there, and a board that does not exist yet cannot be built at all --
    native_sim arrived in 2023, so a 2022 PR cannot use it. Neither fact is in
    the PR itself. The board yaml is kept raw rather than parsed; it is short,
    and the model reads it as written.
    """
    tree = gh(f"repos/{REPO}/git/trees/{base}:boards?recursive=1")
    paths = [b["path"] for b in (tree or {}).get("tree", []) if b.get("type") == "blob"]
    out = {}
    for name, filename in OUR_PLATFORMS.items():
        hits = [p for p in paths if p.rsplit("/", 1)[-1] == filename]
        found = None
        for p in hits:
            f = fetch_file(f"boards/{p}", base)
            if f["content"] and re.search(rf"(?m)^identifier:\s*{re.escape(name)}\s*$", f["content"]):
                found = {"exists": True, "path": f"boards/{p}", "content": f["content"]}
                break
        out[name] = found or {"exists": False}
    if tree and tree.get("truncated"):
        out["_note"] = "boards/ listing was truncated by GitHub; absence may be unreliable"
    return out


def fetch_full(number: int, refresh: bool = False) -> dict | None:
    """Everything a triage model could want about one PR.

    This is what the model reads instead of the GitHub web page, so it has to
    carry the same substance: the code that changed, what reviewers said about
    it, and the bug report it came from. Filenames and a title are not enough
    to judge whether a test detects a bug.
    """
    out = ENRICHED / f"{number}.json"
    if out.exists() and not refresh:
        return json.loads(out.read_text())

    pr = gh(f"repos/{REPO}/pulls/{number}")
    if not pr:
        return None

    # Same files endpoint as fetch, but keeping `patch` this time -- the diff
    # is the single most important field for triage and costs no extra call.
    files = gh_paged(f"repos/{REPO}/pulls/{number}/files", cap=3000)

    # Issues the PR says it fixes. 7 of the 8 known-good PRs link one, and the
    # issue body is usually the actual bug report in the reporter's words.
    linked = []
    for num in dict.fromkeys(ISSUE_REF.findall(pr.get("body") or "")):
        issue = gh(f"repos/{REPO}/issues/{num}")
        if not issue or "pull_request" in issue:
            continue
        linked.append({
            "number": issue["number"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "state": issue["state"],
            "labels": [l["name"] for l in issue.get("labels", [])],
            "comments": [
                {"author": (c.get("user") or {}).get("login"),
                 "body": c.get("body") or ""}
                for c in gh_paged(f"repos/{REPO}/issues/{num}/comments")
            ],
        })

    raw_commits = gh_paged(f"repos/{REPO}/pulls/{number}/commits")
    commits = [{"sha": c["sha"], "message": (c.get("commit") or {}).get("message", "")}
               for c in raw_commits]
    base_commit = None
    if raw_commits:
        parents = raw_commits[0].get("parents") or []
        base_commit = parents[0]["sha"] if parents else None

    record = {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "url": pr["html_url"],
        "state": pr["state"],
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "base_sha": (pr.get("base") or {}).get("sha"),
        "labels": [l["name"] for l in pr.get("labels", [])],
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),

        # The diff itself. `patch` is absent for binary files and for files
        # GitHub considers too large to render -- absent, not empty, so the
        # difference between "no change shown" and "change too big to show"
        # stays visible to whoever reads this.
        "files": [
            {"filename": f["filename"], "status": f["status"],
             "additions": f["additions"], "deletions": f["deletions"],
             "patch": f.get("patch"),
             **({"previous_filename": f["previous_filename"]}
                if f.get("previous_filename") else {})}
            for f in files
        ],
        "files_truncated": len(files) >= 3000,

        "commits": commits,

        # The state of the repo immediately before this PR: the first parent of
        # its first commit.
        #
        # NOT merge_commit_sha~1, which is a different thing entirely -- on a
        # repo as busy as Zephyr that is whatever unrelated PR happened to land
        # just before the merge. PR 65697 shows the gap plainly: its commits are
        # 330c820b (fix) and ba723889 (tests), 330c820b's parent is 41b7c17a and
        # that is the base, while merge_commit_sha is 0e11bcf5 and belongs to
        # neither.
        #
        # head_commit is the PR's last commit, so base_commit..head_commit spans
        # exactly this PR and nothing else, which is the range validation uses
        # to reconstruct the upstream fix.
        "base_commit": base_commit,
        "head_commit": commits[-1]["sha"] if commits else None,

        # Top-level conversation.
        "comments": [
            {"author": (c.get("user") or {}).get("login"),
             "body": c.get("body") or ""}
            for c in gh_paged(f"repos/{REPO}/issues/{number}/comments")
        ],

        # Inline code review. Often the highest-signal content in the whole
        # PR -- this is where a reviewer says the fix misses the root cause.
        "review_comments": [
            {"author": (c.get("user") or {}).get("login"),
             "path": c.get("path"), "line": c.get("line"),
             "diff_hunk": c.get("diff_hunk"), "body": c.get("body") or ""}
            for c in gh_paged(f"repos/{REPO}/pulls/{number}/comments")
        ],

        "reviews": [
            {"author": (r.get("user") or {}).get("login"),
             "state": r.get("state"), "body": r.get("body") or ""}
            for r in gh_paged(f"repos/{REPO}/pulls/{number}/reviews")
            if (r.get("body") or "").strip() or r.get("state") != "COMMENTED"
        ],

        "linked_issues": linked,
    }

    # The complete test suite behind every test file the PR touched.
    #
    # The diff alone is not enough to judge runnability: a PR that adds cases
    # to an existing suite never touches its yaml, so platform_allow and
    # harness would be invisible. Read at the merge commit, which covers both
    # shapes -- for an existing suite the yaml is present at base and merge,
    # for a new suite it exists only at merge.
    ref = pr.get("merge_commit_sha") or pr["head"]["sha"]
    cache, suites = {}, {}
    for f in record["files"]:
        name = f["filename"]
        if not name.startswith("tests/"):
            continue
        root, marker = find_suite_root(name, ref, cache)
        if root and root not in suites:
            suites[root] = {"config": marker, "files": fetch_suite(root, ref)}
    record["test_suites"] = suites

    # Read at the base commit, not the merge: these describe the tree the
    # agent is given, before the change.
    if base_commit:
        record["base_sources"] = fetch_base_sources(record["files"], base_commit)
        record["platforms"] = fetch_platforms(base_commit)

    ENRICHED.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2))
    return record


# ---------------------------------------------------------------- commands


def cmd_selftest(_args) -> None:
    """Every known-good PR must survive the filters."""
    print(f"Checking the {len(KNOWN_GOOD)} hand-validated instances "
          f"against the hard filters.\n")
    failures = []
    for n in KNOWN_GOOD:
        pr = fetch_pr(n)
        if not pr:
            print(f"  [ERROR ] {n}  could not fetch")
            failures.append((n, "fetch failed"))
            continue
        kept, reason = classify(pr)
        print(f"  [{'KEEP  ' if kept else 'REJECT'}] {n}  {reason}")
        if not kept:
            failures.append((n, reason))

    print()
    if failures:
        print(f"  {len(failures)}/{len(KNOWN_GOOD)} known-good PRs were "
              f"rejected. The filter is too strict:")
        for n, r in failures:
            print(f"    {n}: {r}")
        sys.exit(1)
    print(f"  {len(KNOWN_GOOD)}/{len(KNOWN_GOOD)} pass. Filters are safe.")


def cmd_fetch(args) -> None:
    numbers = args.pr or search_merged_prs(args.since, args.until)
    print(f"{len(numbers)} merged PRs to fetch\n")
    for i, n in enumerate(numbers, 1):
        got = fetch_pr(n)
        mark = "ok" if got else "FAIL"
        print(f"  [{i}/{len(numbers)}] {n} {mark}")
    print(f"\ncached under {CACHE.relative_to(REPO_ROOT)}")


def cmd_filter(_args) -> None:
    files = sorted(CACHE.glob("*.json"))
    if not files:
        sys.exit("No cached PRs. Run `fetch` first.")

    kept, reasons = [], {}
    for f in files:
        pr = json.loads(f.read_text())
        ok, reason = classify(pr)
        if ok:
            kept.append(summarise(pr))
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as fh:
        for rec in sorted(kept, key=lambda r: r["number"]):
            fh.write(json.dumps(rec) + "\n")

    print(f"  {len(files)} cached  ->  {len(kept)} candidates "
          f"({len(kept)/len(files)*100:.0f}%)\n")
    if reasons:
        print("  rejected:")
        for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {c:>5}  {r}")
    print(f"\n  written to {OUT.relative_to(REPO_ROOT)}")



def describe_context(rec: dict) -> str:
    """The two sections read at the base commit, for the progress line."""
    if "base_sources" not in rec:
        return "no base-commit context (enriched before it existed; use --refresh)"
    src = rec["base_sources"]
    kept = sum(1 for f in src.values() if f.get("content") is not None)
    plats = [k for k, v in rec.get("platforms", {}).items()
             if not k.startswith("_") and v.get("exists")]
    return (f"{kept}/{len(src)} pre-change sources, "
            f"platforms at base: {', '.join(plats) or 'none'}")


def cmd_enrich(args) -> None:
    if args.pr:
        numbers = args.pr
    else:
        if not OUT.exists():
            sys.exit("No candidates.jsonl. Run `filter` first.")
        numbers = [json.loads(l)["number"] for l in open(OUT)]
        if args.limit:
            numbers = numbers[:args.limit]

    print(f"Enriching {len(numbers)} candidates with full GitHub context.\n")
    ok = 0
    for i, n in enumerate(numbers, 1):
        rec = fetch_full(n, refresh=args.refresh)
        if not rec:
            print(f"  [{i}/{len(numbers)}] {n}  FAILED")
            continue
        ok += 1
        patched = sum(1 for f in rec["files"] if f.get("patch"))
        suites = rec.get("test_suites", {})
        nfiles = sum(len(s["files"]) for s in suites.values())
        print(f"  [{i}/{len(numbers)}] {n}  "
              f"{patched}/{len(rec['files'])} files with diff, "
              f"{len(rec['commits'])} commits, "
              f"{len(rec['comments']) + len(rec['review_comments'])} comments, "
              f"{len(rec['linked_issues'])} linked issues, "
              f"{len(suites)} suite(s)/{nfiles} files, "
              f"{describe_context(rec)}")

    print(f"\n  {ok}/{len(numbers)} enriched  ->  "
          f"{ENRICHED.relative_to(REPO_ROOT)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="check the 8 known-good PRs survive")

    f = sub.add_parser("fetch", help="download PRs into the local cache")
    f.add_argument("--pr", type=int, nargs="+", help="specific PR numbers")
    f.add_argument("--since", default="2024-01-01")
    f.add_argument("--until", default="2024-12-31")

    sub.add_parser("filter", help="apply hard filters to the cache")

    e = sub.add_parser("enrich",
                       help="full GitHub context for PRs that passed filtering")
    e.add_argument("--pr", type=int, nargs="+", help="specific PR numbers")
    e.add_argument("--limit", type=int, help="only the first N candidates")
    e.add_argument("--refresh", action="store_true",
                   help="re-fetch records that already exist instead of reusing them")

    args = p.parse_args()
    {"selftest": cmd_selftest, "fetch": cmd_fetch,
     "filter": cmd_filter, "enrich": cmd_enrich}[args.cmd](args)


if __name__ == "__main__":
    main()
