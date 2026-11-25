import unittest

from measurements import measurement_basis


class MeasurementBasisTests(unittest.TestCase):
    def test_scaled_context_uses_explicit_si_units(self):
        result = measurement_basis(0.01, {"authority": "EPSG::32637"}, True)
        self.assertTrue(result["scaled"])
        self.assertEqual(result["units"]["area"], "square_meters")
        self.assertEqual(result["warnings"], [])

    def test_unscaled_and_missing_dem_are_never_reported_as_metric(self):
        result = measurement_basis(None, None, False)
        self.assertFalse(result["scaled"])
        self.assertEqual(result["units"]["length"], "chunk_units")
        self.assertEqual(len(result["warnings"]), 2)

    def test_model_measurement_does_not_warn_about_missing_dem(self):
        result = measurement_basis(1.0, None, False, dem_required=False)
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
