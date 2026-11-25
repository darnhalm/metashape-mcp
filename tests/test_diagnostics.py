import unittest

from diagnostics import numeric_summary, path_diagnostics, unaligned_runs


class DiagnosticMathTests(unittest.TestCase):
    def test_numeric_summary(self):
        result = numeric_summary([-2, 0, 2])
        self.assertEqual(3, result["count"])
        self.assertEqual(0, result["mean"])
        self.assertAlmostEqual((8 / 3) ** 0.5, result["rms"])

    def test_relative_gaps_use_observed_median(self):
        rows = [
            {"key": 1, "label": "1", "position": [0, 0, 0]},
            {"key": 2, "label": "2", "position": [1, 0, 0]},
            {"key": 3, "label": "3", "position": [2, 0, 0]},
            {"key": 4, "label": "4", "position": [12, 0, 0]},
        ]
        result = path_diagnostics(rows)
        self.assertEqual(1, result["relative_gap_count"])
        self.assertEqual(10, result["relative_gaps"][0]["distance"])
        self.assertEqual([12, 0, 0], result["extent"]["size"])

    def test_unaligned_runs(self):
        rows = [
            {"label": "a", "aligned": True},
            {"label": "b", "aligned": False},
            {"label": "c", "aligned": False},
            {"label": "d", "aligned": True},
            {"label": "e", "aligned": False},
        ]
        self.assertEqual([2, 1], [item["count"] for item in unaligned_runs(rows)])


if __name__ == "__main__":
    unittest.main()
