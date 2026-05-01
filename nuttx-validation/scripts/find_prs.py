#!/usr/bin/env python3
"""
PR Finder for NuttX Sim Pipeline

Searches apache/nuttx for merged PRs that:
  1. Are merged and not a draft/WIP
  2. Have a linked issue (GitHub linked:issue filter)
  3. Mention a runnable target in the PR body (sim:* or rv-virt:*)
  4. Touch sim-compatible kernel subsystems (not hardware/arch only)
  5. Have a total line diff under 1000 lines

Usage:
    python3 find_prs.py              # find up to 50 candidates (default)
    python3 find_prs.py --count 20   # find fewer
    python3 find_prs.py --start 15000 --end 20000  # restrict PR number range

Output:
    - Results table printed to terminal
    - results/candidate_prs.json written
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env file if present
# ---------------------------------------------------------------------------

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _val.strip())

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

KERNEL_REPO = "apache/nuttx"
APPS_REPO   = "apache/nuttx-apps"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Throttle: stay well under GitHub's 5000 req/hour primary limit
_MIN_CALL_INTERVAL = 0.72  # ~83 calls/min
_last_call = 0.0


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_get(url, params=None):
    """GET with proactive throttle and reactive rate-limit backoff."""
    global _last_call
    gap = _MIN_CALL_INTERVAL - (time.time() - _last_call)
    if gap > 0:
        time.sleep(gap)
    _last_call = time.time()

    for attempt in range(5):
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code in (403, 429):
            retry_after = int(r.headers.get("Retry-After", 60))
            print(f"  Rate limited (HTTP {r.status_code}) — waiting {retry_after}s...")
            time.sleep(retry_after)
            _last_call = time.time()
            continue
        return r
    return None


def search_merged_prs(page):
    """Search apache/nuttx for merged PRs with linked issues, server-side filtered."""
    url = "https://api.github.com/search/issues"
    query = f"repo:{KERNEL_REPO} is:pr is:merged linked:issue"
    params = {
        "q": query,
        "sort": "updated",
        "order": "desc",
        "per_page": 100,
        "page": page,
    }
    r = gh_get(url, params)
    if r and r.status_code == 200:
        return r.json().get("items", [])
    return []


def get_pr_files(repo, pr_number):
    """Return list of {filename, status, additions, deletions} dicts for files changed in a PR."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    r = gh_get(url, {"per_page": 100})
    if r and r.status_code == 200:
        return [{"filename": f["filename"], "status": f["status"],
                 "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
                for f in r.json()]
    return []




# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Linked issue in kernel PR body: closes/fixes/resolves #N
LINKED_ISSUE_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#(\d+)",
    re.IGNORECASE,
)

# Matches NuttX board:config patterns in PR body (e.g. sim:nsh, stm32f4discovery:nsh, qemu-armv7a:nsh)
# Rules: both parts lowercase, board >= 3 chars, config starts with letter and >= 2 chars
BOARD_CONFIG_RE = re.compile(r'\b([a-z][a-z0-9\-]{2,}:[a-z][a-z0-9]{1,})\b')

# Architectures we do NOT have toolchains for in the base image.
# PRs where ALL kernel source changes are under these prefixes cannot be built.
UNSUPPORTED_ARCH_PREFIXES = (
    "arch/avr/",      # AVR — needs avr-gcc
    "arch/ceva/",     # CEVA DSP — proprietary toolchain
    "arch/hc/",       # HC08/HC12 — needs m68k/hc toolchain
    "arch/mips/",     # MIPS — needs mips-elf-gcc
    "arch/misoc/",    # MiSoC FPGA — niche toolchain
    "arch/or1k/",     # OpenRISC 1000 — needs or1k-elf-gcc
    "arch/renesas/",  # Renesas RX/M32C — needs Renesas toolchain
    "arch/sparc/",    # SPARC — needs sparc-elf-gcc
    "arch/tricore/",  # Infineon TriCore — needs tricore-gcc
    "arch/xtensa/",   # ESP32/ESP8266 — needs xtensa-esp32-elf
    "arch/z16/",      # Zilog Z16F — needs z16f toolchain
    "arch/z80/",      # Z80 — needs sdcc
)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_pr(kernel_pr):
    """
    Evaluate a single kernel PR. Returns a result dict if it passes all
    filters, or (None, reason) if rejected.
    """
    pr_number = kernel_pr["number"]
    title = kernel_pr.get("title", "")
    body = kernel_pr.get("body", "") or ""
    merged_at = (kernel_pr.get("pull_request") or {}).get("merged_at") or kernel_pr.get("closed_at", "")

    # Filter 1: skip draft/WIP/RFC (free)
    if any(tag in title.upper() for tag in ("[RFC]", "[WIP]", "[POC]", "[DRAFT]")):
        return None, "draft_wip"

    # Filter 2: body must mention at least one runnable target (sim:* or rv-virt:*)
    # These are the only targets confirmed runnable end-to-end in our pipeline.
    board_configs = BOARD_CONFIG_RE.findall(body)
    runnable = [b for b in board_configs if b.startswith("sim:") or b.startswith("rv-virt:")]
    if not runnable:
        return None, "no_runnable_target"

    # Extract linked issue numbers for display
    issue_matches = LINKED_ISSUE_RE.findall(body)

    # Filter 3: kernel sources must not be entirely in unsupported architectures (1 API call)
    kernel_files = get_pr_files(KERNEL_REPO, pr_number)

    # Filter 4: total line diff must be under 1000 lines
    total_diff = sum(f["additions"] + f["deletions"] for f in kernel_files)
    if total_diff > 1000:
        return None, "diff_too_large"

    kernel_sources = [
        f["filename"] for f in kernel_files
        if f["filename"].endswith((".c", ".h", ".S", ".cpp"))
    ]
    if kernel_sources:
        unsupported_sources = [
            f for f in kernel_sources
            if any(f.startswith(p) for p in UNSUPPORTED_ARCH_PREFIXES)
        ]
        if len(unsupported_sources) == len(kernel_sources):
            return None, "unsupported_arch"

    # All filters passed
    linked_issues = list(dict.fromkeys(issue_matches))
    # Deduplicate runnable targets, preserve order
    seen = set()
    targets = [b for b in runnable if not (b in seen or seen.add(b))]

    return {
        "pr_number":      pr_number,
        "title":          title,
        "url":            kernel_pr["html_url"],
        "merged_at":      merged_at,
        "linked_issues":  [f"https://github.com/{KERNEL_REPO}/issues/{n}" for n in linked_issues],
        "targets":        targets,
        "kernel_sources": kernel_sources,
    }, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Find NuttX PR candidates for sim pipeline")
    parser.add_argument("--count", type=int, default=50,
                        help="Target number of candidate PRs to find (default: 50)")
    parser.add_argument("--start", type=int, default=None,
                        help="Only consider PRs with number >= this value")
    parser.add_argument("--end", type=int, default=None,
                        help="Only consider PRs with number <= this value")
    args = parser.parse_args()

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set. Add it to scripts/.env or export it.")
        sys.exit(1)

    results_file = Path(__file__).parent.parent / "results" / "candidate_prs.json"
    results_file.parent.mkdir(exist_ok=True)

    # Load previously saved results so interrupted runs can be resumed
    results = []
    seen_kernel_prs = set()
    if results_file.exists():
        try:
            existing = json.loads(results_file.read_text())
            results = existing
            seen_kernel_prs = {r["pr_number"] for r in results}
            if results:
                print(f"Resuming — {len(results)} results already saved.\n")
        except Exception:
            pass

    reject_counts = {}

    print("=" * 65)
    print("  NuttX PR Finder — Sim Pipeline")
    print(f"  Target: {args.count} candidates")
    if args.start or args.end:
        print(f"  PR range: {args.start or 'any'} – {args.end or 'any'}")
    print("=" * 65)
    print()

    page = 1
    evaluated = 0

    while len(results) < args.count:
        print(f"Fetching page {page} of kernel PRs...")
        prs = search_merged_prs(page)
        if not prs:
            print("No more PRs to fetch.")
            break

        for pr in prs:
            pr_number = pr["number"]

            # Apply optional range filter
            if args.start and pr_number < args.start:
                continue
            if args.end and pr_number > args.end:
                continue

            # Skip already evaluated
            if pr_number in seen_kernel_prs:
                continue

            seen_kernel_prs.add(pr_number)
            evaluated += 1

            result, reason = evaluate_pr(pr)

            if result:
                results.append(result)
                results_file.write_text(json.dumps(results, indent=2))
                print(f"  PASS  nuttx#{pr_number}")
                print(f"        {result['title'][:60]}")
                print(f"        Targets: {result['targets'][:3]}")
                print()
                if len(results) >= args.count:
                    break
            else:
                reject_counts[reason] = reject_counts.get(reason, 0) + 1
                print(f"  skip  nuttx#{pr_number:6d}  [{reason}]  {pr.get('title','')[:50]}")

        page += 1

    # Summary
    print()
    print("=" * 65)
    print(f"  DONE — {len(results)} candidates found  ({evaluated} PRs evaluated)")
    print("=" * 65)

    if reject_counts:
        print(f"\n  Rejection breakdown:")
        for reason, count in sorted(reject_counts.items(), key=lambda x: -x[1]):
            print(f"    {count:5d}  {reason}")

    print(f"\n  Results:\n")
    for i, r in enumerate(results, 1):
        print(f"{i:2}. nuttx#{r['pr_number']}")
        print(f"    Title  : {r['title']}")
        print(f"    URL    : {r['url']}")
        print(f"    Merged : {r['merged_at'][:10]}")
        for issue in r["linked_issues"]:
            print(f"    Issue  : {issue}")
        print(f"    Targets: {r['targets'][:3]}")
        print(f"    Sources: {r['kernel_sources'][:3]}")
        print()

    print(f"Saved to {results_file}")


if __name__ == "__main__":
    main()
