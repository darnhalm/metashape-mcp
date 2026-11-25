import unittest

from sensor_calibration import CALIBRATION_PARAMETERS, validate_sensor_calibration_plan


class SensorCalibrationValidationTests(unittest.TestCase):
    def test_explicit_changes_are_preserved(self):
        plan = validate_sensor_calibration_plan(
            4, "/work/lens.xml", "fisheye", False, ["f", "cx", "cy"])
        self.assertEqual(plan["sensor_key"], 4)
        self.assertEqual(plan["sensor_type"], "fisheye")
        self.assertEqual(plan["fixed_params"], ["F", "Cx", "Cy"])
        self.assertIn("p2", CALIBRATION_PARAMETERS)

    def test_empty_fixed_params_explicitly_clears_list(self):
        self.assertEqual(validate_sensor_calibration_plan(0, fixed_params=[])["fixed_params"], [])

    def test_invalid_or_empty_plans_are_rejected(self):
        cases = [
            {"sensor_key": 0},
            {"sensor_key": True, "fixed": True},
            {"sensor_key": 0, "sensor_type": "rpc"},
            {"sensor_key": 0, "fixed": "yes"},
            {"sensor_key": 0, "fixed_params": ["f", "f"]},
            {"sensor_key": 0, "fixed_params": ["unknown"]},
            {"sensor_key": 0, "fixed": True, "fixed_params": ["f"]},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_sensor_calibration_plan(**values)


if __name__ == "__main__":
    unittest.main()
