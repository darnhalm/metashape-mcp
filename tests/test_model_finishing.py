import unittest

from model_finishing import plan_warnings, validate_steps


class ValidateStepsTests(unittest.TestCase):
    def test_defaults_and_order(self):
        steps = validate_steps([{"operation": "remove_components", "min_faces": 50},
                                {"operation": "close_holes"},
                                {"operation": "smooth", "strength": 2},
                                {"operation": "decimate", "face_count": 5000}])
        self.assertEqual([s["operation"] for s in steps],
                         ["remove_components", "close_holes", "smooth", "decimate"])
        self.assertEqual(steps[1]["level"], 30)
        self.assertEqual((steps[2]["strength"], steps[2]["fix_borders"]), (2.0, True))

    def test_rejections(self):
        bad = [[], [{"operation": "explode"}], [{"operation": "decimate"}],
               [{"operation": "decimate", "face_count": 10}],
               [{"operation": "close_holes", "level": 0}],
               [{"operation": "smooth", "strength": True}],
               [{"operation": "fix_topology", "extra": 1}],
               [{"operation": "fix_topology"}] * 11]
        for steps in bad:
            with self.assertRaises(ValueError, msg=steps):
                validate_steps(steps)

    def test_warnings(self):
        steps = validate_steps([{"operation": "close_holes"}, {"operation": "decimate", "face_count": 20000}])
        warnings = plan_warnings(steps, {"faces": 15000, "textures": 1})
        self.assertEqual(len(warnings), 3)
        self.assertEqual(plan_warnings(validate_steps([{"operation": "fix_topology"}]),
                                       {"faces": 10, "textures": 0}), [])


if __name__ == "__main__":
    unittest.main()


class FaceSelectionStepTests(unittest.TestCase):
    def test_new_steps(self):
        steps = validate_steps([{"operation": "remove_low_confidence", "max_percent": 10},
                                {"operation": "crop_box", "min": [0, 0, 0], "max": [1, 2, 3]}])
        self.assertEqual(steps[1]["max"], [1.0, 2.0, 3.0])
        for bad in ([{"operation": "remove_low_confidence", "max_percent": 0}],
                    [{"operation": "remove_box", "min": [0, 0], "max": [1, 1, 1]}],
                    [{"operation": "crop_box", "min": [2, 0, 0], "max": [1, 1, 1]}]):
            with self.assertRaises(ValueError):
                validate_steps(bad)
