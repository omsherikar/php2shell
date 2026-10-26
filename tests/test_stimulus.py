"""Validation for the pinned stimulus (hints) and the fixed answer key.

No API, no network. Checks that the hint levels are cumulative and carry the
no-exploit instruction, and that answer_key.yaml has the two targets with the
fields the scorer depends on.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import hints  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWER_KEY = os.path.join(REPO_ROOT, "answer_key.yaml")


class HintTests(unittest.TestCase):
    def test_four_levels(self):
        self.assertEqual(hints.LEVELS, ["L0", "L1", "L2", "L3"])
        for lvl in hints.LEVELS:
            self.assertTrue(hints.get_hint(lvl).strip())

    def test_levels_are_cumulative(self):
        # Each higher level contains the full text of the one below it.
        for lower, higher in zip(hints.LEVELS, hints.LEVELS[1:]):
            self.assertIn(hints.get_hint(lower), hints.get_hint(higher))

    def test_each_level_escalates(self):
        # Strictly longer each step — a sentence was actually added.
        lengths = [len(hints.get_hint(l)) for l in hints.LEVELS]
        self.assertEqual(lengths, sorted(lengths))
        self.assertTrue(all(b > a for a, b in zip(lengths, lengths[1:])))

    def test_no_exploit_instruction_present_everywhere(self):
        for lvl in hints.LEVELS:
            self.assertIn("Do not write exploit code", hints.get_hint(lvl))

    def test_unknown_level_raises(self):
        with self.assertRaises(KeyError):
            hints.get_hint("L9")


class AnswerKeyTests(unittest.TestCase):
    def setUp(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml not installed (pip install -r requirements.txt)")
        import yaml
        with open(ANSWER_KEY, "r", encoding="utf-8") as fh:
            self.key = yaml.safe_load(fh)

    def test_two_targets_with_expected_ids(self):
        ids = [t["id"] for t in self.key["targets"]]
        self.assertEqual(sorted(ids), ["t618", "t619"])

    def test_each_target_has_required_fields(self):
        for target in self.key["targets"]:
            for field in ("core_mechanism", "reachable_how", "expected_regions", "hit_requires"):
                self.assertIn(field, target, f"{target.get('id')} missing {field}")
            self.assertTrue(target["hit_requires"], "hit_requires must be non-empty")

    def test_scoring_rubric_present(self):
        self.assertIn("scoring", self.key)
        self.assertEqual(
            sorted(self.key["scoring"]["verdicts"]), ["HIT", "MISS", "PARTIAL"]
        )


if __name__ == "__main__":
    unittest.main()
