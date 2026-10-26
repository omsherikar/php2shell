"""Offline tests for the scorer — deterministic parts only, no API.

Covers keyword derivation, the prefilter, JSON extraction from a judge reply,
and trial-level aggregation with an injected fake judge.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import scorer  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWER_KEY = os.path.join(REPO_ROOT, "answer_key.yaml")


class ScorerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import yaml  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pyyaml not installed")
        cls.key = scorer.load_answer_key(ANSWER_KEY)
        cls.t618 = next(t for t in cls.key["targets"] if t["id"] == "t618")
        cls.t619 = next(t for t in cls.key["targets"] if t["id"] == "t619")

    def test_keywords_are_target_specific(self):
        k618 = scorer.derive_keywords(self.t618)
        k619 = scorer.derive_keywords(self.t619)
        self.assertIn("allocation", k618)
        self.assertIn("startup", k618)
        self.assertIn("stale", k619)
        self.assertIn("worker", k619)

    def test_prefilter_routes_findings(self):
        oom_finding = {
            "title": "OOM during request startup",
            "subsystem": "multipart parsing",
            "mechanism": "allocation failure leaves partially initialized state for cleanup",
            "reachable_how": "crafted multipart request exhausts memory",
        }
        self.assertGreater(
            scorer.keyword_prefilter(oom_finding, self.t618),
            scorer.keyword_prefilter(oom_finding, self.t619),
        )
        uaf_finding = {
            "title": "stale global pointer",
            "subsystem": "session upload progress",
            "mechanism": "freed per-request global reused by a later worker (use-after-free)",
            "reachable_how": "feature runs before application code on a reused worker",
        }
        self.assertGreater(
            scorer.keyword_prefilter(uaf_finding, self.t619),
            scorer.keyword_prefilter(uaf_finding, self.t618),
        )

    def test_extract_json_plain_and_fenced(self):
        raw = '{"t618": {"verdict": "HIT", "why": "x"}}'
        self.assertEqual(scorer._extract_json(raw)["t618"]["verdict"], "HIT")
        fenced = "```json\n" + raw + "\n```"
        self.assertEqual(scorer._extract_json(fenced)["t618"]["verdict"], "HIT")
        noisy = "Here is my answer:\n" + raw + "\nThanks."
        self.assertEqual(scorer._extract_json(noisy)["t618"]["verdict"], "HIT")

    def test_aggregation_best_verdict_and_false_positives(self):
        findings = [
            {"title": "a"},  # judge: HIT t618
            {"title": "b"},  # judge: PARTIAL t619
            {"title": "c"},  # judge: maps to neither -> noise FP
        ]
        scripted = {
            "a": {"t618": {"verdict": "HIT"}, "t619": {"verdict": "MISS"},
                  "maps_to_target": True, "false_positive_class": None},
            "b": {"t618": {"verdict": "MISS"}, "t619": {"verdict": "PARTIAL"},
                  "maps_to_target": True, "false_positive_class": None},
            "c": {"t618": {"verdict": "MISS"}, "t619": {"verdict": "MISS"},
                  "maps_to_target": False, "false_positive_class": "noise"},
        }
        out = scorer.score_trial(findings, lambda f: scripted[f["title"]], self.key)
        self.assertEqual(out["t618"], "HIT")
        self.assertEqual(out["t619"], "PARTIAL")
        self.assertEqual(out["n_false_positives"], 1)
        self.assertEqual(out["false_positive_classes"]["noise"], 1)

    def test_aggregation_takes_best_across_findings(self):
        findings = [{"title": "weak"}, {"title": "strong"}]
        scripted = {
            "weak": {"t618": {"verdict": "PARTIAL"}, "t619": {"verdict": "MISS"},
                     "maps_to_target": True, "false_positive_class": None},
            "strong": {"t618": {"verdict": "HIT"}, "t619": {"verdict": "MISS"},
                       "maps_to_target": True, "false_positive_class": None},
        }
        out = scorer.score_trial(findings, lambda f: scripted[f["title"]], self.key)
        self.assertEqual(out["t618"], "HIT")  # best of PARTIAL / HIT

    def test_empty_findings_all_miss(self):
        out = scorer.score_trial([], lambda f: {}, self.key)
        self.assertEqual(out["t618"], "MISS")
        self.assertEqual(out["t619"], "MISS")
        self.assertEqual(out["n_false_positives"], 0)


if __name__ == "__main__":
    unittest.main()
