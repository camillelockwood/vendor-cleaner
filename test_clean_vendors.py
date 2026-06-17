"""
Unit tests for the deterministic parts of clean_vendors.

Run with:   python test_clean_vendors.py
(No API key needed — these test the plain-Python logic, not the model.)
"""

import unittest
import clean_vendors as cv


class TestBasicClean(unittest.TestCase):
    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(cv.basic_clean("  Acme   Co  "), "Acme Co")

    def test_normalizes_comma_spacing(self):
        self.assertEqual(cv.basic_clean("Smith ,John"), "Smith, John")


class TestBlockingKey(unittest.TestCase):
    def test_variants_share_a_key(self):
        # These three should all reduce to the same fingerprint.
        a = cv.blocking_key("Dennis K. Burke, Inc.")
        b = cv.blocking_key("Dennis K Burke Inc")
        c = cv.blocking_key("DENNIS K. BURKE")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_different_vendors_differ(self):
        self.assertNotEqual(cv.blocking_key("Acme LLC"), cv.blocking_key("Globex LLC"))


class TestParseAmount(unittest.TestCase):
    def test_strips_symbols(self):
        self.assertAlmostEqual(cv.parse_amount("$1,234.56"), 1234.56)

    def test_bad_input_is_zero(self):
        self.assertEqual(cv.parse_amount("n/a"), 0.0)
        self.assertEqual(cv.parse_amount(""), 0.0)


class TestMergeOverlapping(unittest.TestCase):
    def test_groups_sharing_a_name_merge(self):
        groups = [["A", "B"], ["B", "C"], ["X", "Y"]]
        merged = cv.merge_overlapping(groups)
        merged_sets = sorted([sorted(g) for g in merged])
        self.assertIn(["A", "B", "C"], merged_sets)
        self.assertIn(["X", "Y"], merged_sets)


class TestDecide(unittest.TestCase):
    def test_invalid_canonical_forces_review(self):
        # Model returns a name not in the group -> must NOT auto-apply.
        d = {"names": ["Acme Inc", "Acme Inc."], "same_entity": True,
             "confidence": "high", "canonical_name": "Totally Different Name"}
        cname, apply_group = cv.decide(d)
        self.assertIn(cname, d["names"])   # falls back to a real name
        self.assertFalse(apply_group)      # and is flagged, not applied

    def test_valid_high_confidence_applies(self):
        d = {"names": ["Acme Inc", "Acme Inc."], "same_entity": True,
             "confidence": "high", "canonical_name": "Acme Inc."}
        cname, apply_group = cv.decide(d)
        self.assertEqual(cname, "Acme Inc.")
        self.assertTrue(apply_group)


if __name__ == "__main__":
    unittest.main(verbosity=2)
