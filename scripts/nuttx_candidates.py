#!/usr/bin/env python3
"""Fetch and cache the NuttX PR records the per-apps-PR triage reads.

    python scripts/nuttx_candidates.py enrich-apps --ostest-scan
    python scripts/nuttx_candidates.py enrich-apps --pr 3790 2327
    python scripts/nuttx_candidates.py enrich-kernel --pr 20112

`enrich-apps` writes apps-only records for the per-apps-PR triage, which
finds kernel partners itself; `enrich-kernel` caches a kernel PR it proposes.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent.parent
# EMBEDEVAL_NUTTX_DATA moves the data directory, e.g. to one per parallel
# run; EMBEDEVAL_NUTTX_DATA_RO names a shared, pre-warmed copy that cached()
# reads from but never writes.
DATA = Path(os.environ.get("EMBEDEVAL_NUTTX_DATA") or ROOT / "candidates" / "nuttx")
DATA_RO = Path(os.environ["EMBEDEVAL_NUTTX_DATA_RO"]) if os.environ.get("EMBEDEVAL_NUTTX_DATA_RO") \
    else None
# Every merged apps PR with its changed paths (GitHub GraphQL scan).
SCAN = DATA / "apps_merged_file_scan.json"
# Apps-only records for the per-apps-PR triage, and cached GitHub PRs,
# issues and commits it follows, under github/<repo name>/.
APPS_RECORDS = DATA / "apps_records"
GITHUB_CACHE = DATA / "github"
APPS = "apache/nuttx-apps"
KERNEL = "apache/nuttx"

KERNEL_LINK = re.compile(
    r"(?:https?://github\.com/)?apache/nuttx/(?:pull/|pulls/)?(\d+)\b"
    r"|#apache/nuttx/(\d+)\b"
    r"|apache/nuttx#(\d+)\b",
    re.IGNORECASE,
)
APPS_LINK = re.compile(
    r"(?:https?://github\.com/)?apache/nuttx-apps/(?:pull/|pulls/)?(\d+)\b"
    r"|#apache/nuttx-apps/(\d+)\b"
    r"|apache/nuttx-apps#(\d+)\b",
    re.IGNORECASE,
)


def kernel_pr_refs(body: str) -> list[int]:
    """Scoped kernel references; apache/nuttx#N still needs type resolution."""
    numbers = []
    for match in KERNEL_LINK.finditer(body or ""):
        # URLs to /issues/ are intentionally excluded by the pattern.
        numbers.append(int(next(group for group in match.groups() if group)))
    return list(dict.fromkeys(numbers))


def apps_pr_refs(body: str) -> list[int]:
    return list(dict.fromkeys(
        int(next(group for group in match.groups() if group))
        for match in APPS_LINK.finditer(body or "")
    ))


def gh(path: str, retries: int = 3):
    for attempt in range(retries):
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        error = proc.stderr.lower()
        if "rate limit" in error or "submitted too quickly" in error:
            time.sleep(20 * (attempt + 1))
            continue
        if "unexpected eof" in error or "connection reset" in error or "http 5" in error \
                or "timed out" in error:
            time.sleep(attempt + 1)
            continue
        raise RuntimeError(f"gh api {path}: {proc.stderr.strip()[:300]}")
    raise RuntimeError(f"gh api {path}: failed after {retries} attempts: {proc.stderr.strip()[:300]}")


def closing_issues(number: int) -> list[int]:
    query = """query($number: Int!) {
      repository(owner: "apache", name: "nuttx") {
        pullRequest(number: $number) {
          closingIssuesReferences(first: 100) {
            nodes { number repository { nameWithOwner } }
          }
        }
      }
    }"""
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"number={number}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"closing issue query for #{number}: {proc.stderr.strip()[:300]}")
    nodes = json.loads(proc.stdout)["data"]["repository"]["pullRequest"][
        "closingIssuesReferences"]["nodes"]
    return [node["number"] for node in nodes
            if node.get("repository", {}).get("nameWithOwner") == KERNEL]


def paged(path: str, cap: int = 3000) -> list:
    result = []
    for page in range(1, (cap + 99) // 100 + 1):
        separator = "&" if "?" in path else "?"
        batch = gh(f"{path}{separator}per_page=100&page={page}")
        result.extend(batch)
        if len(batch) < 100:
            break
    return result


def full_pr(repo: str, number: int) -> dict:
    pr = gh(f"repos/{repo}/pulls/{number}")
    files = paged(f"repos/{repo}/pulls/{number}/files")
    commits = paged(f"repos/{repo}/pulls/{number}/commits", cap=1000)
    first_parents = (commits[0].get("parents") or []) if commits else []
    return {
        "repo": repo, "number": number, "url": pr["html_url"],
        "title": pr["title"], "body": pr.get("body") or "",
        "created_at": pr.get("created_at"), "merged_at": pr.get("merged_at"),
        "labels": [label["name"] for label in pr.get("labels", [])],
        "additions": pr.get("additions"), "deletions": pr.get("deletions"),
        "merge_commit_sha": pr.get("merge_commit_sha"),
        "base_commit": first_parents[0]["sha"] if first_parents else None,
        "head_commit": commits[-1]["sha"] if commits else None,
        "commits": [{"sha": c["sha"],
                     "message": (c.get("commit") or {}).get("message", "")}
                    for c in commits],
        "files": [{"filename": f["filename"], "status": f["status"],
                   "previous_filename": f.get("previous_filename"),
                   "additions": f.get("additions"),
                   "deletions": f.get("deletions"), "patch": f.get("patch")}
                  for f in files],
        "files_truncated": len(files) >= 3000,
        "comments": paged(f"repos/{repo}/issues/{number}/comments", cap=1000),
        "review_comments": paged(f"repos/{repo}/pulls/{number}/comments", cap=1000),
        "reviews": paged(f"repos/{repo}/pulls/{number}/reviews", cap=1000),
    }


def issue_refs(body: str) -> list[int]:
    pattern = re.compile(
        r"https?://github\.com/apache/nuttx/issues/(\d+)\b"
        r"|\b(?:fixes|closes|resolves|issue)\s+#(\d+)\b",
        re.IGNORECASE,
    )
    return list(dict.fromkeys(
        int(next(group for group in match.groups() if group))
        for match in pattern.finditer(body or "")
    ))


def source_at(repo: str, path: str, ref: str) -> str | None:
    try:
        blob = gh(f"repos/{repo}/contents/{quote(path)}?ref={ref}")
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    if not isinstance(blob, dict) or blob.get("encoding") != "base64":
        return None
    if blob.get("size", 0) > 100_000:
        return None
    try:
        return base64.b64decode(blob["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def enrich_kernel(kernel: dict, sources: bool = True) -> dict:
    """Add linked issues and, with sources, pre-fix file contents."""
    ref = kernel["number"]
    issue_numbers = list(dict.fromkeys([
        *issue_refs(kernel["body"]), *closing_issues(ref)
    ]))
    issues = []
    for issue_number in issue_numbers:
        issue = gh(f"repos/{KERNEL}/issues/{issue_number}")
        if "pull_request" not in issue:
            issues.append({"number": issue_number, "title": issue["title"],
                           "body": issue.get("body") or "", "url": issue["html_url"],
                           "state": issue.get("state"),
                           "labels": [label["name"] for label in issue.get("labels", [])],
                           "comments": paged(f"repos/{KERNEL}/issues/{issue_number}/comments",
                                             cap=1000)})
    kernel["linked_issues"] = issues
    if not sources:
        return kernel
    kernel["base_sources"] = {}
    for file in kernel["files"]:
        if file["status"] == "added" or file["filename"].startswith("Documentation/"):
            continue
        old_path = file.get("previous_filename") or file["filename"]
        kernel["base_sources"][old_path] = source_at(KERNEL, old_path, kernel["base_commit"])
    kernel["sim_nsh_defconfig"] = source_at(
        KERNEL, "boards/sim/sim/sim/configs/nsh/defconfig", kernel["base_commit"]
    )
    return kernel


def changed_test_contents(apps: dict) -> dict:
    # Triage can read other files at the pinned apps commit if needed.
    contents = {}
    for file in apps["files"]:
        path = file["filename"]
        if not path.startswith("testing/") or file["status"] == "removed":
            continue
        contents[path] = source_at(APPS, path, apps["head_commit"])
    if any(path.startswith("testing/ostest/") for path in contents):
        for path in ("testing/ostest/Makefile", "testing/ostest/CMakeLists.txt",
                     "testing/ostest/ostest.h", "testing/ostest/ostest_main.c"):
            if path not in contents:
                contents[path] = source_at(APPS, path, apps["head_commit"])
    return contents


def ostest_apps_prs(scan: Path = SCAN) -> list[int]:
    """Merged apps PRs that change any path under testing/ostest/."""
    prs = json.loads(scan.read_text())["prs"]
    return sorted(int(number) for number, pr in prs.items()
                  if any(node["path"].startswith("testing/ostest/")
                         for node in pr["files"]["nodes"]))


def cached(path: Path, fetch, refresh: bool = False):
    """JSON at path, or fetch() written there; the write is atomic so
    parallel triage cases can share the cache. A copy under DATA_RO is read
    when path is missing."""
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    if DATA_RO and not refresh:
        with contextlib.suppress(ValueError):
            shared = DATA_RO / path.relative_to(DATA)
            if shared.exists():
                return json.loads(shared.read_text())
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)
    return value


def timeline_refs(repo: str, number: int) -> list[dict]:
    """Cross-references to a PR or issue, and commits that mention it."""
    events = []
    for event in paged(f"repos/{repo}/issues/{number}/timeline", cap=1000):
        if event.get("event") == "cross-referenced":
            issue = (event.get("source") or {}).get("issue") or {}
            events.append({
                "event": "cross-referenced", "created_at": event.get("created_at"),
                "repo": (issue.get("repository_url") or "").rsplit("/repos/", 1)[-1],
                "number": issue.get("number"), "title": issue.get("title"),
                "is_pr": "pull_request" in issue, "state": issue.get("state"),
                "merged_at": (issue.get("pull_request") or {}).get("merged_at"),
                "url": issue.get("html_url")})
        elif event.get("event") == "referenced" and event.get("commit_id"):
            events.append({"event": "referenced", "created_at": event.get("created_at"),
                           "commit_id": event["commit_id"],
                           "commit_url": event.get("commit_url")})
    return events


def pr_record(repo: str, number: int, refresh: bool = False) -> dict:
    """A full PR with its timeline, cached; kernel PRs also get linked
    issues (not pre-fix sources: tools read those from the clone)."""
    def fetch():
        pr = {**full_pr(repo, number), "timeline": timeline_refs(repo, number)}
        return enrich_kernel(pr, sources=False) if repo == KERNEL else pr
    return cached(GITHUB_CACHE / repo.split("/")[1] / f"pr-{number}.json", fetch, refresh)


def kernel_record(number: int) -> dict:
    """A kernel PR found by search rather than by link, enriched on demand."""
    return pr_record(KERNEL, number)


def issue_or_pr(repo: str, number: int) -> dict:
    """A PR record, or an issue with comments and timeline; "kind" says which."""
    folder = GITHUB_CACHE / repo.split("/")[1]
    path = folder / f"issue-{number}.json"
    if path.exists():
        return {"kind": "issue", **json.loads(path.read_text())}
    if not (folder / f"pr-{number}.json").exists():
        issue = gh(f"repos/{repo}/issues/{number}")   # GitHub serves PRs here too
        if "pull_request" not in issue:
            record = {"repo": repo, "number": number, "url": issue["html_url"],
                      "title": issue["title"], "body": issue.get("body") or "",
                      "state": issue.get("state"), "created_at": issue.get("created_at"),
                      "labels": [label["name"] for label in issue.get("labels", [])],
                      "comments": paged(f"repos/{repo}/issues/{number}/comments", cap=1000),
                      "timeline": timeline_refs(repo, number)}
            return {"kind": "issue", **cached(path, lambda: record)}
    return {"kind": "pr", **pr_record(repo, number)}


def commit_record(repo: str, sha: str) -> dict:
    """A commit's message and files from GitHub, and the PRs it belongs to."""
    def fetch():
        commit = gh(f"repos/{repo}/commits/{sha}")
        return {"repo": repo, "sha": commit["sha"], "url": commit.get("html_url"),
                "message": (commit.get("commit") or {}).get("message", ""),
                "date": ((commit.get("commit") or {}).get("committer") or {}).get("date"),
                "parents": [parent["sha"] for parent in commit.get("parents", [])],
                "files": [{"filename": f["filename"], "status": f["status"],
                           "additions": f.get("additions"), "deletions": f.get("deletions"),
                           "patch": f.get("patch")} for f in commit.get("files", [])],
                "prs": [{"number": pr["number"], "title": pr["title"],
                         "merged_at": pr.get("merged_at"), "url": pr["html_url"]}
                        for pr in gh(f"repos/{repo}/commits/{sha}/pulls")]}
    return cached(GITHUB_CACHE / repo.split("/")[1] / f"commit-{sha}.json", fetch)


def apps_record(number: int, refresh: bool = False) -> dict:
    """One apps PR for the per-apps-PR triage: the full PR, its timeline and
    the changed test sources, with no kernel partner chosen."""
    def fetch():
        apps = pr_record(APPS, number, refresh)
        return {"project": "nuttx", "apps": apps,
                "test_contents": changed_test_contents(apps)}
    return cached(APPS_RECORDS / f"{number}.json", fetch, refresh)


def dispatch(args) -> None:
    if args.command == "enrich-apps":
        numbers = list(dict.fromkeys((args.pr or [])
                                     + (ostest_apps_prs(args.scan) if args.ostest_scan else [])))
        if not numbers:
            sys.exit("give --pr and/or --ostest-scan")
        failed = []
        for index, number in enumerate(numbers, 1):
            try:
                record = apps_record(number, args.refresh)
            except RuntimeError as error:   # one bad PR must not stop a long batch
                failed.append(number)
                print(f"[{index}/{len(numbers)}] apps #{number}: {error}", flush=True)
                continue
            print(f"[{index}/{len(numbers)}] apps #{number}: "
                  f"{len(record['apps']['files'])} files, "
                  f"{len(record['apps']['timeline'])} timeline refs", flush=True)
        print(f"{len(numbers) - len(failed)} records in {APPS_RECORDS}"
              + (f"; failed: {failed}" if failed else ""))
        if failed:
            sys.exit(1)
    else:
        for number in args.pr:
            record = kernel_record(number)
            print(f"kernel #{number}: {record['title']} (merged {record['merged_at']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    apps = sub.add_parser("enrich-apps", help="apps-only records for per-apps-PR triage")
    apps.add_argument("--pr", type=int, nargs="+")
    apps.add_argument("--ostest-scan", action="store_true",
                      help="every merged apps PR in --scan that changes testing/ostest/")
    apps.add_argument("--scan", type=Path, default=SCAN)
    apps.add_argument("--refresh", action="store_true")
    kernel = sub.add_parser("enrich-kernel", help="cache kernel PRs a triage proposed")
    kernel.add_argument("--pr", type=int, nargs="+", required=True)
    dispatch(parser.parse_args())


if __name__ == "__main__":
    main()
