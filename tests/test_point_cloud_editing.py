import unittest

from point_cloud_editing import validate_point_cloud_cleanup


class PointCloudCleanupValidationTests(unittest.TestCase):
    def test_class_and_confidence_plans(self):
        classes = validate_point_cloud_cleanup(3, "classes", [7, 2])
        self.assertEqual(classes["point_classes"], [2, 7])
        confidence = validate_point_cloud_cleanup(
            3, "confidence", [0, 2], 0, 3, 0.5)
        self.assertEqual(confidence["min_confidence"], 0)
        self.assertEqual(confidence["max_selected_fraction"], 0.5)

    def test_rejects_ambiguous_or_dangerous_parameters(self):
        cases = [
            (True, "classes", [0], None, None, 0.3),
            (1, "classes", [], None, None, 0.3),
            (1, "classes", [0, 0], None, None, 0.3),
            (1, "classes", [128], None, None, 0.3),
            (1, "classes", [0], 0, 3, 0.3),
            (1, "confidence", [], None, 3, 0.3),
            (1, "confidence", [], 4, 3, 0.3),
            (1, "confidence", [], 0, 3, 0),
            (1, "other", [], None, None, 0.3),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_point_cloud_cleanup(*args)


if __name__ == "__main__":
    unittest.main()
