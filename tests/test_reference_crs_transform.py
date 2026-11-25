import unittest

from reference_crs_transform import validate_reference_crs_transform


class ReferenceCrsTransformValidationTests(unittest.TestCase):
    def test_explicit_safe_defaults(self):
        plan = validate_reference_crs_transform(4326, 32637)
        self.assertTrue(plan["transform_cameras"])
        self.assertTrue(plan["transform_markers"])
        self.assertFalse(plan["set_chunk_crs"])
        self.assertFalse(plan["update_transform"])

    def test_rejects_equal_invalid_and_empty(self):
        cases = [
            (4326, 4326, {}), (True, 32637, {}), (4326, 0, {}),
            (4326, 32637, {"transform_cameras": False, "transform_markers": False}),
            (4326, 32637, {"update_transform": 1}),
        ]
        for source, target, options in cases:
            with self.subTest(source=source, target=target, options=options):
                with self.assertRaises(ValueError):
                    validate_reference_crs_transform(source, target, **options)


if __name__ == "__main__":
    unittest.main()
