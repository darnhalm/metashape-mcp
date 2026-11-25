import unittest
import math

from camera_reference_exif import validate_reference_exif_plan


class CameraReferenceExifValidationTests(unittest.TestCase):
    def test_explicit_wgs84_and_accuracy(self):
        plan = validate_reference_exif_plan(
            4326, [5, 5, 10], load_rotation=True,
            rotation_accuracy_deg=[10, 10, 15], enable_rotations=True)
        self.assertEqual(plan["fallback_accuracy_m"], [5.0, 5.0, 10.0])
        self.assertEqual(plan["rotation_accuracy_deg"], [10.0, 10.0, 15.0])

    def test_rejects_implicit_crs_accuracy_and_rotation(self):
        cases = [
            (3857, [1, 1, 1], {}),
            (4326, None, {}),
            (4326, [1, 0, 1], {}),
            (4326, [1, math.nan, 1], {}),
            (4326, [1, 1, 1], {"load_rotation": True}),
            (4326, [1, 1, 1], {"rotation_accuracy_deg": [1, 1, 1]}),
            (4326, [1, 1, 1], {"enable_rotations": True}),
        ]
        for epsg, accuracy, options in cases:
            with self.subTest(epsg=epsg, accuracy=accuracy, options=options), self.assertRaises(ValueError):
                validate_reference_exif_plan(epsg, accuracy, **options)


if __name__ == "__main__":
    unittest.main()
