import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import nuttx_ga as ga  # noqa: E402

MARKERS = {"numbers": {"2137", "10934"}, "hashes": {"083c87b39a"}, "files": {"smp_call.c"}}


class GuardTests(unittest.TestCase):
    def test_gold_pr_numbers_hashes_and_file_names_are_rejected(self):
        self.assertEqual(ga.guard("Check apps #2137 first", MARKERS), ["PR number 2137"])
        self.assertEqual(ga.guard("kernel PR 10934.", MARKERS), ["PR number 10934"])
        self.assertEqual(ga.guard("see 083C87B39A or 083c87b", MARKERS), ["commit hash 083c87b39a"])
        self.assertEqual(ga.guard("read smp_call.c", MARKERS), ["file name smp_call.c"])

    def test_general_text_passes(self):
        self.assertEqual(ga.guard("Open at most 40 files; 21370 lines; my_smp_call.cc", MARKERS), [])


class RenderAndRankTests(unittest.TestCase):
    RULES = {"rules": {"a": {"section": 2, "text": "RULE A"}, "b": {"section": 5, "text": "RULE B"},
                       "c": {"section": 2, "text": "RULE C"}}}

    def test_rules_land_at_the_end_of_their_section_in_order(self):
        text = ga.render_prompt(["c", "a", "b"], self.RULES)
        self.assertLess(text.index("RULE C"), text.index("RULE A"))
        self.assertLess(text.index("RULE A"), text.index("## 3. "))
        self.assertGreater(text.index("RULE B"), text.index("## 5. "))
        self.assertEqual(ga.render_prompt([], self.RULES), (ga.GA / "base_prompt.md").read_text().rstrip("\n") + "\n")

    def test_ranking_uses_mean_then_worst_then_blind(self):
        def score(correct, scored=13):
            return {"correct": correct, "scored": scored}
        scores = {}
        for sha, normals, blinds in (("aaa", [13, 11], [9, 9]), ("bbb", [12, 12], [5, 5]),
                                     ("ccc", [13, 11], [7, 7])):
            for rep, (n, b) in enumerate(zip(normals, blinds), 1):
                scores[f"01/{sha}/{rep}/normal"] = score(n)
                scores[f"01/{sha}/{rep}/blind"] = score(b, 9)
        ranked = ga.ranking({"scores": scores})
        # aaa, bbb and ccc all average 12; bbb's worst repeat is best, then aaa beats ccc on blind
        self.assertEqual([f["sha"] for f in ranked], ["bbb", "aaa", "ccc"])
        fit = {f["sha"]: f for f in ranked}
        self.assertEqual(fit["aaa"]["perfect"], 1)

    def test_perfect_requires_all_thirteen_normal_cases(self):
        scores = {"00/aaa/1/normal": {"correct": 12, "scored": 12},
                  "00/aaa/2/normal": {"correct": 13, "scored": 13}}
        self.assertEqual(ga.fitness({"scores": scores}, "aaa")["perfect"], 1)


if __name__ == "__main__":
    unittest.main()
