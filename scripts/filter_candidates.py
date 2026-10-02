#!/usr/bin/env python3
"""
Hard filters for Zephyr and Apache Mynewt PR candidates.

    python scripts/filter_candidates.py selftest --repo mynewt
    python scripts/filter_candidates.py fetch --repo mynewt --since 2024-01-01 --until 2024-06-30
    python scripts/filter_candidates.py filter --repo mynewt
    python scripts/filter_candidates.py enrich --repo mynewt

Three stages, deliberately separate:

    fetch    cheap, wide   -- title, body, labels, file list. Everything the
                             hard filters need and nothing more.
    filter   free, instant -- cuts the pool to PRs that could become instances.
    enrich   costly, narrow-- full GitHub context for the survivors only:
                             diffs, review threads, linked issues, commits,
                             and the complete test suite each PR touches.

Enrich runs last because only ~15% of PRs survive filtering. Pulling the
expensive context for the whole pool would be about six times the API calls
for data that is thrown away. The hard filters use none of it.

Deliberately permissive. These filters exist to cut the pool down to PRs that
*could* become an instance, not to judge whether one should. Judgement is the
model's job in the triage stage -- anything rejected here is rejected forever
and never reaches it, so the bar is set as low as it can usefully go.

That is not conservatism for its own sake. Every filter is measured against
the instances already validated by hand for that project. For Zephyr, most
apparently obvious stricter filters threw good instances away:

    "adds a new test file"   rejects 5 of 8  (most fixes add cases to an
                                              existing test file)
    has the `bug` label      rejects 4 of 8
    <= 10 files changed      rejects 74435, which has 21
    has a linked issue       rejects 43405

So the rule is: a filter belongs here only if it cannot reject any known-good
instance for that project. `selftest` enforces exactly that -- run it after
any change.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_ROOT = REPO_ROOT / "candidates"

PROJECTS = {
    "zephyr": {
        "github_repo": "zephyrproject-rtos/zephyr",
        "known_good": [33690, 43405, 62109, 65697, 74435, 82272, 85079, 89534],
    },
    "mynewt": {
        "github_repo": "apache/mynewt-core",
        "known_good": [2809, 3299, 3680],
    },
}

CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
CLOSING_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 100) {
        nodes {
          number
          repository { nameWithOwner }
        }
      }
    }
  }
}
"""


def project_config(project: str) -> dict:
    try:
        return PROJECTS[project]
    except KeyError:
        raise ValueError(f"unknown project {project!r}; known: {sorted(PROJECTS)}")


def project_paths(project: str) -> tuple[Path, Path, Path]:
    """Keep Zephyr's original cache layout while isolating Mynewt records."""
    if project == "zephyr":
        root = CANDIDATES_ROOT
    else:
        root = CANDIDATES_ROOT / project
    return root / "cache", root / "candidates.jsonl", root / "enriched"


def is_test_path(path: str, project: str) -> bool:
    if project == "zephyr":
        return path.startswith("tests/")
    parts = path.split("/")
    return any(part == "selftest" or part.startswith("selftest-") for part in parts)


def is_source_path(path: str, project: str) -> bool:
    if is_test_path(path, project):
        return False
    if project == "zephyr":
        return not path.startswith(("doc/", ".github/", "samples/"))
    return not path.startswith(("docs/", ".github/", "test/"))


def test_root_hint(path: str, project: str) -> str:
    if project == "zephyr":
        return "/".join(path.split("/")[:3])
    parts = path.split("/")
    for index, part in enumerate(parts):
        if part == "selftest" or part.startswith("selftest-"):
            return "/".join(parts[: index + 1])
    return str(Path(path).parent)


def issue_numbers_from_body(body: str, repo: str) -> list[int]:
    """Same-repository issues linked by closing syntax or a direct URL."""
    escaped_repo = re.escape(repo)
    target = (
        rf"(?:#|{escaped_repo}#|"
        rf"https://github\.com/{escaped_repo}/issues/)(\d+)\b"
    )
    pattern = re.compile(rf"(?i)\b{CLOSING_KEYWORD}\s*:?\s+{target}")
    numbers = [int(match.group(1)) for match in pattern.finditer(body)]
    issue_url = re.compile(
        rf"(?i)https://github\.com/{escaped_repo}/issues/(\d+)\b"
    )
    numbers.extend(int(match.group(1)) for match in issue_url.finditer(body))
    return list(dict.fromkeys(numbers))


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


def gh_graphql(query: str, variables: dict, retries: int = 3):
    """Run a read-only GitHub GraphQL query through the authenticated gh CLI."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        cmd += ["-F", f"{key}={value}"]
    for attempt in range(retries):
        proc = subprocess.run(cmd, capture_output=True, text=True)
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


def linked_issue_numbers(repo: str, pr_number: int, body: str) -> list[int]:
    """Combine textual closing references with GitHub's linked-issue data."""
    numbers = issue_numbers_from_body(body, repo)
    owner, name = repo.split("/", 1)
    result = gh_graphql(
        CLOSING_ISSUES_QUERY,
        {"owner": owner, "name": name, "number": pr_number},
    )
    try:
        nodes = result["data"]["repository"]["pullRequest"][
            "closingIssuesReferences"
        ]["nodes"]
    except (KeyError, TypeError):
        nodes = []
    numbers.extend(
        node["number"]
        for node in nodes
        if node.get("number")
        and (node.get("repository") or {}).get("nameWithOwner") == repo
    )
    return list(dict.fromkeys(numbers))


def fetch_pr(number: int, project: str = "zephyr") -> dict | None:
    """Fetch one PR plus its file list, and cache it."""
    repo = project_config(project)["github_repo"]
    cache, _, _ = project_paths(project)
    cached = cache / f"{number}.json"
    if cached.exists():
        return json.loads(cached.read_text())

    pr = gh(f"repos/{repo}/pulls/{number}")
    if not pr:
        return None

    # Paginate. 100 is the maximum page size this endpoint allows, so a single
    # request silently returns only the first 100 files of a larger PR. GitHub
    # stops serving files past 3000; that is a real ceiling, unlike page size.
    files, page = [], 1
    while True:
        batch = gh(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
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
             "additions": f["additions"], "deletions": f["deletions"],
             "previous_filename": f.get("previous_filename")}
            for f in files
        ],
        # Only true at GitHub's hard 3000-file ceiling, where the list really
        # is incomplete and nothing we do can complete it.
        "files_truncated": len(files) >= 3000,
    }
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(record, indent=2))
    return record


def search_merged_prs(since: str, until: str, project: str = "zephyr") -> list[int]:
    """PR numbers merged in a date window. Windows must stay under 1000
    results -- that is a hard cap in GitHub's search API, and exceeding it
    silently truncates rather than erroring."""
    numbers, page = [], 1
    repo = project_config(project)["github_repo"]
    q = f"repo:{repo}+is:pr+is:merged+merged:{since}..{until}"
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


def classify(pr: dict, project: str = "zephyr") -> tuple[bool, str]:
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

    tests = [n for n in names if is_test_path(n, project)]
    if not tests:
        reason = "touches no tests/" if project == "zephyr" else "touches no native selftest package"
        return False, reason

    source = [n for n in names if is_source_path(n, project)]
    if not source:
        reason = (
            "touches no source outside tests/"
            if project == "zephyr"
            else "touches no source outside native selftests"
        )
        return False, reason

    return True, "ok"


def summarise(pr: dict, project: str = "zephyr") -> dict:
    """The record handed to the triage stage."""
    repo = project_config(project)["github_repo"]
    names = [f["filename"] for f in pr["files"]]
    tests = [n for n in names if is_test_path(n, project)]
    source = [n for n in names if is_source_path(n, project)]
    return {
        "project": project,
        "number": pr["number"],
        "title": pr["title"],
        "url": f"https://github.com/{repo}/pull/{pr['number']}",
        "merged_at": pr["merged_at"],
        "merge_commit_sha": pr["merge_commit_sha"],
        "labels": pr["labels"],
        "n_files": len(names),
        "source_files": source,
        "test_files": tests,
        "test_dirs": sorted({test_root_hint(t, project) for t in tests}),
    }



# ---------------------------------------------------------------- enrichment


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


def find_suite_root(
    file_path: str, sha: str, cache: dict, project: str = "zephyr"
) -> tuple[str | None, str | None]:
    """Walk up from a changed test file to the directory that owns it.

    tests/posix/common/src/key.c -> tests/posix/common

    The depth varies -- tests live in src/ under some suites and at the root
    of others -- so this cannot be a fixed number of path segments.
    """
    repo = project_config(project)["github_repo"]
    parts = file_path.split("/")
    for depth in range(len(parts) - 1, 1, -1):     # deepest first, stop above tests/
        d = "/".join(parts[:depth])
        if d not in cache:
            listing = gh(f"repos/{repo}/contents/{d}?ref={sha}")
            names = {f["name"] for f in listing} if isinstance(listing, list) else set()
            if project == "zephyr":
                cache[d] = next((m for m in SUITE_MARKERS if m in names), None)
            else:
                cache[d] = "pkg.yml" if "pkg.yml" in names else None
        if cache[d]:
            return d, cache[d]
    return None, None


def fetch_suite(root: str, sha: str, project: str = "zephyr") -> dict:
    """Every file in a test suite directory, with contents.

    No size cap. Most Zephyr suites are four files; the occasional large
    shared suite is a problem for later, not now.
    """
    repo = project_config(project)["github_repo"]
    tree = gh(f"repos/{repo}/git/trees/{sha}:{root}?recursive=1")
    if not tree:
        return {}
    out = {}
    for b in tree.get("tree", []):
        if b.get("type") != "blob":
            continue
        blob = gh(f"repos/{repo}/git/blobs/{b['sha']}")
        content = None
        if blob and blob.get("encoding") == "base64":
            try:
                content = base64.b64decode(blob["content"]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                content = None          # binary; recorded by name and size only
        out[b["path"]] = {"size": b.get("size"), "content": content}
    return out


def fetch_full(number: int, project: str = "zephyr") -> dict | None:
    """Everything a triage model could want about one PR.

    This is what the model reads instead of the GitHub web page, so it has to
    carry the same substance: the code that changed, what reviewers said about
    it, and the bug report it came from. Filenames and a title are not enough
    to judge whether a test detects a bug.
    """
    repo = project_config(project)["github_repo"]
    _, _, enriched = project_paths(project)
    out = enriched / f"{number}.json"
    if out.exists():
        return json.loads(out.read_text())

    pr = gh(f"repos/{repo}/pulls/{number}")
    if not pr:
        return None

    # Same files endpoint as fetch, but keeping `patch` this time -- the diff
    # is the single most important field for triage and costs no extra call.
    files = gh_paged(f"repos/{repo}/pulls/{number}/files", cap=3000)

    # Issues linked through GitHub or the PR's closing-keyword syntax. The
    # issue body is usually the actual bug report in the reporter's words.
    linked = []
    for num in linked_issue_numbers(repo, number, pr.get("body") or ""):
        issue = gh(f"repos/{repo}/issues/{num}")
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
                for c in gh_paged(f"repos/{repo}/issues/{num}/comments")
            ],
        })

    raw_commits = gh_paged(f"repos/{repo}/pulls/{number}/commits")
    commits = [{"sha": c["sha"], "message": (c.get("commit") or {}).get("message", "")}
               for c in raw_commits]
    base_commit = None
    if raw_commits:
        parents = raw_commits[0].get("parents") or []
        base_commit = parents[0]["sha"] if parents else None

    record = {
        "project": project,
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
             "previous_filename": f.get("previous_filename")}
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
            for c in gh_paged(f"repos/{repo}/issues/{number}/comments")
        ],

        # Inline code review. Often the highest-signal content in the whole
        # PR -- this is where a reviewer says the fix misses the root cause.
        "review_comments": [
            {"author": (c.get("user") or {}).get("login"),
             "path": c.get("path"), "line": c.get("line"),
             "diff_hunk": c.get("diff_hunk"), "body": c.get("body") or ""}
            for c in gh_paged(f"repos/{repo}/pulls/{number}/comments")
        ],

        "reviews": [
            {"author": (r.get("user") or {}).get("login"),
             "state": r.get("state"), "body": r.get("body") or ""}
            for r in gh_paged(f"repos/{repo}/pulls/{number}/reviews")
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
        if not is_test_path(name, project):
            continue
        root, marker = find_suite_root(name, ref, cache, project)
        if root and root not in suites:
            suites[root] = {"config": marker, "files": fetch_suite(root, ref, project)}
    record["test_suites"] = suites

    enriched.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2))
    return record


# ---------------------------------------------------------------- commands


def cmd_selftest(args) -> None:
    """Every known-good PR must survive the filters."""
    known_good = project_config(args.repo)["known_good"]
    print(f"Checking the {len(known_good)} hand-validated {args.repo} instances "
          f"against the hard filters.\n")
    failures = []
    for n in known_good:
        pr = fetch_pr(n, args.repo)
        if not pr:
            print(f"  [ERROR ] {n}  could not fetch")
            failures.append((n, "fetch failed"))
            continue
        kept, reason = classify(pr, args.repo)
        print(f"  [{'KEEP  ' if kept else 'REJECT'}] {n}  {reason}")
        if not kept:
            failures.append((n, reason))

    print()
    if failures:
        print(f"  {len(failures)}/{len(known_good)} known-good PRs were "
              f"rejected. The filter is too strict:")
        for n, r in failures:
            print(f"    {n}: {r}")
        sys.exit(1)
    print(f"  {len(known_good)}/{len(known_good)} pass. Filters are safe.")


def cmd_fetch(args) -> None:
    cache, _, _ = project_paths(args.repo)
    numbers = args.pr or search_merged_prs(args.since, args.until, args.repo)
    print(f"{len(numbers)} merged PRs to fetch\n")
    for i, n in enumerate(numbers, 1):
        got = fetch_pr(n, args.repo)
        mark = "ok" if got else "FAIL"
        print(f"  [{i}/{len(numbers)}] {n} {mark}")
    print(f"\ncached under {cache.relative_to(REPO_ROOT)}")


def cmd_filter(args) -> None:
    cache, out, _ = project_paths(args.repo)
    files = sorted(cache.glob("*.json"))
    if not files:
        sys.exit("No cached PRs. Run `fetch` first.")

    kept, reasons = [], {}
    for f in files:
        pr = json.loads(f.read_text())
        ok, reason = classify(pr, args.repo)
        if ok:
            kept.append(summarise(pr, args.repo))
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        for rec in sorted(kept, key=lambda r: r["number"]):
            fh.write(json.dumps(rec) + "\n")

    print(f"  {len(files)} cached  ->  {len(kept)} candidates "
          f"({len(kept)/len(files)*100:.0f}%)\n")
    if reasons:
        print("  rejected:")
        for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {c:>5}  {r}")
    print(f"\n  written to {out.relative_to(REPO_ROOT)}")



def cmd_enrich(args) -> None:
    _, out, enriched = project_paths(args.repo)
    if args.pr:
        numbers = args.pr
    else:
        if not out.exists():
            sys.exit("No candidates.jsonl. Run `filter` first.")
        numbers = [json.loads(l)["number"] for l in open(out)]
        if args.limit:
            numbers = numbers[:args.limit]

    print(f"Enriching {len(numbers)} candidates with full GitHub context.\n")
    ok = 0
    for i, n in enumerate(numbers, 1):
        rec = fetch_full(n, args.repo)
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
              f"{len(suites)} suite(s)/{nfiles} files")

    print(f"\n  {ok}/{len(numbers)} enriched  ->  "
          f"{enriched.relative_to(REPO_ROOT)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_repo(parser) -> None:
        parser.add_argument("--repo", choices=PROJECTS, default="zephyr")

    s = sub.add_parser("selftest", help="check known-good PRs survive")
    add_repo(s)

    f = sub.add_parser("fetch", help="download PRs into the local cache")
    add_repo(f)
    f.add_argument("--pr", type=int, nargs="+", help="specific PR numbers")
    f.add_argument("--since", default="2024-01-01")
    f.add_argument("--until", default="2024-12-31")

    f2 = sub.add_parser("filter", help="apply hard filters to the cache")
    add_repo(f2)

    e = sub.add_parser("enrich",
                       help="full GitHub context for PRs that passed filtering")
    add_repo(e)
    e.add_argument("--pr", type=int, nargs="+", help="specific PR numbers")
    e.add_argument("--limit", type=int, help="only the first N candidates")

    args = p.parse_args()
    {"selftest": cmd_selftest, "fetch": cmd_fetch,
     "filter": cmd_filter, "enrich": cmd_enrich}[args.cmd](args)


if __name__ == "__main__":
    main()
