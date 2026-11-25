import unittest

from tie_point_filtering import (DEFAULT_MAX_SELECTED_FRACTION, optimization_parameters,
                                 validate_max_selected_fraction, validate_passes)


class TiePointFilteringValidationTests(unittest.TestCase):
    def test_defaults_and_order_are_preserved(self):
        passes = validate_passes([
            {"criterion": "reconstruction_uncertainty", "threshold": 10},
            {"criterion": "reprojection_error", "threshold": 0.5, "optimize": False},
        ])
        self.assertEqual(passes[0], {"criterion": "reconstruction_uncertainty",
                                     "threshold": 10.0, "optimize": True})
        self.assertFalse(passes[1]["optimize"])
        self.assertEqual(DEFAULT_MAX_SELECTED_FRACTION, 0.30)

    def test_invalid_passes_and_fraction_are_rejected(self):
        for passes in ([], [{"criterion": "image_count", "threshold": 2}],
                       [{"criterion": "reprojection_error", "threshold": 0}],
                       [{"criterion": "reprojection_error", "threshold": 1, "extra": 2}]):
            with self.subTest(passes=passes), self.assertRaises(ValueError):
                validate_passes(passes)
        for value in (0, 0.91, True, "0.3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_max_selected_fraction(value)

    def test_camera_optimization_parameters_are_explicit(self):
        parameters = optimization_parameters(False, True, True)
        self.assertFalse(parameters["fit_cx"])
        self.assertTrue(parameters["fit_b1"])
        self.assertTrue(parameters["adaptive_fitting"])
        self.assertFalse(parameters["fit_k4"])


if __name__ == "__main__":
    unittest.main()
