"""Candidate pairing checks for linked NuttX OS and ostest PRs."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nuttx_candidates as candidates


class LinkedOstestPairsTests(unittest.TestCase):
    def test_links_from_either_pr_make_distinct_pair_leads(self):
        apps = {
            "number": 10, "title": "add regression", "merged_at": "2024-01-02",
            "body": "See apache/nuttx#20",
            "files": [{"filename": "testing/ostest/example.c", "status": "modified"}],
        }

        def github(path):
            number = int(path.rsplit("/", 1)[-1])
            return {"number": number, "merged_at": "2024-01-01",
                    "body": "See apache/nuttx-apps#10" if number == 22 else "",
                    "html_url": f"https://github.com/apache/nuttx/pull/{number}"}

        def comments(path, cap=3000):
            if path == "repos/apache/nuttx-apps/issues/10/comments":
                return [{"body": "Also https://github.com/apache/nuttx/pull/21"}]
            return []

        with patch.object(candidates, "gh", side_effect=github), \
             patch.object(candidates, "paged", side_effect=comments), \
             patch.object(candidates, "inverse_kernel_refs", return_value=[22, 23]):
            rows = candidates.linked_ostest_pairs(apps)

        self.assertEqual([(row["kernel_pr"], row["link_sources"]) for row in rows], [
            (20, ["apps_body"]), (21, ["apps_comment"]), (22, ["kernel_body"])
        ])
        self.assertEqual([row["ostest_paths"] for row in rows],
                         [["testing/ostest/example.c"]] * 3)

    def test_non_ostest_change_does_not_start_link_search(self):
        apps = {"number": 10, "title": "other test", "merged_at": "2024-01-02",
                "body": "See apache/nuttx#20",
                "files": [{"filename": "testing/nettest/example.c", "status": "modified"}]}
        with patch.object(candidates, "inverse_kernel_refs",
                          side_effect=AssertionError("searched")):
            self.assertEqual(candidates.linked_ostest_pairs(apps), [])

    def test_kernel_first_fetches_linked_apps_and_keeps_only_ostest(self):
        kernel = {"number": 20, "merged_at": "2024-01-01",
                  "body": "Test: apache/nuttx-apps#10",
                  "html_url": "https://github.com/apache/nuttx/pull/20"}
        apps = {
            10: {"number": 10, "title": "test", "merged_at": "2024-01-02",
                 "files": [{"filename": "testing/ostest/example.c", "status": "modified"}]},
            11: {"number": 11, "title": "other", "merged_at": "2024-01-02",
                 "files": [{"filename": "testing/nettest/example.c", "status": "modified"}]},
        }

        def comments(path, cap=3000):
            if path == "repos/apache/nuttx/issues/20/comments":
                return [{"body": "Also apache/nuttx-apps#11"}]
            return []

        with patch.object(candidates, "gh", return_value=kernel), \
             patch.object(candidates, "paged", side_effect=comments), \
             patch.object(candidates, "fetch_apps", side_effect=apps.get) as fetch:
            rows = candidates.linked_apps_from_kernel(20)

        self.assertEqual([call.args[0] for call in fetch.call_args_list], [10, 11])
        self.assertEqual([(row["apps_pr"], row["link_sources"]) for row in rows],
                         [(10, ["kernel_body"])])

    def test_kernel_first_follows_apps_pr_timeline_mentions(self):
        kernel = {"number": 20, "merged_at": "2024-01-01", "body": "",
                  "html_url": "https://github.com/apache/nuttx/pull/20"}
        apps = {"number": 10, "title": "test", "merged_at": "2024-01-02",
                "files": [{"filename": "testing/ostest/example.c", "status": "modified"}]}

        def timeline_source(number, repository, is_pr=True):
            issue = {"number": number,
                     "repository_url": f"https://api.github.com/repos/{repository}"}
            if is_pr:
                issue["pull_request"] = {"url": "pull request"}
            return {"event": "cross-referenced", "source": {"issue": issue}}

        def activity(path, cap=3000):
            if path == "repos/apache/nuttx/issues/20/timeline":
                return [timeline_source(10, "apache/nuttx-apps"),
                        timeline_source(11, "apache/nuttx-apps", is_pr=False),
                        timeline_source(12, "someone/other")]
            return []

        with patch.object(candidates, "gh", return_value=kernel), \
             patch.object(candidates, "paged", side_effect=activity), \
             patch.object(candidates, "fetch_apps", return_value=apps) as fetch:
            rows = candidates.linked_apps_from_kernel(20)

        fetch.assert_called_once_with(10)
        self.assertEqual([(row["apps_pr"], row["link_sources"]) for row in rows],
                         [(10, ["kernel_timeline"])])

    def test_date_window_collects_both_repos_and_deduplicates(self):
        from_apps = {"case": "apps-10-kernel-20", "apps_pr": 10, "kernel_pr": 20,
                     "ostest_paths": ["testing/ostest/example.c"],
                     "link_sources": ["apps_body"]}
        from_kernel = {**from_apps, "link_sources": ["kernel_comment"]}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "pairs.jsonl"
            args = SimpleNamespace(command="pairs", pr=None, kernel_pr=None,
                                   since="2024-01-01", until="2024-01-31", out=out)
            with patch.object(candidates, "search_merged", side_effect=[[10], [20]]) as search, \
                 patch.object(candidates, "fetch_apps", return_value={"number": 10}), \
                 patch.object(candidates, "linked_ostest_pairs", return_value=[from_apps]), \
                 patch.object(candidates, "linked_apps_from_kernel",
                              return_value=[from_kernel]):
                candidates.dispatch(args)
            rows = [json.loads(line) for line in out.read_text().splitlines()]

        self.assertEqual([call.args[0] for call in search.call_args_list],
                         [candidates.APPS, candidates.KERNEL])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["link_sources"], ["apps_body", "kernel_comment"])

    def test_inverse_link_search_rejects_incomplete_results(self):
        with patch.object(candidates, "gh", return_value={
                "incomplete_results": True, "total_count": 1, "items": []}):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                candidates.inverse_kernel_refs(10)


if __name__ == "__main__":
    unittest.main()
