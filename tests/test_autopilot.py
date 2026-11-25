import unittest

from autopilot import notification_text, required_inputs, validate_steps


class AutopilotTests(unittest.TestCase):
    def test_full_chain_from_photos(self):
        steps = validate_steps([{"task": "MatchPhotos", "parameters": {"downscale": 2}}, {"task": "AlignCameras"},
                                {"task": "BuildDepthMaps"}, {"task": "BuildModel"}, {"task": "BuildUV"},
                                {"task": "BuildTexture"}])
        self.assertEqual(required_inputs(steps), [])

    def test_inputs_from_source(self):
        steps = validate_steps([{"task": "BuildModel"}, {"task": "BuildDem"}, {"task": "BuildOrthomosaic"}])
        self.assertEqual(required_inputs(steps), ["depth_maps", "point_cloud"])

    def test_rejections(self):
        for steps in ([], [{"task": "ExportModel"}], [{"task": "SaveProject"}],
                      [{"task": "BuildModel", "parameters": {"path": "/x"}}],
                      [{"task": "BuildModel", "extra": 1}], [{"task": "BuildModel"}] * 21):
            with self.assertRaises(ValueError, msg=steps):
                validate_steps(steps)
        with self.assertRaises(ValueError):
            required_inputs(validate_steps([{"task": "AlignCameras"}]))

    def test_notification(self):
        self.assertIn("ошибка", notification_text("autopilot", "failed", "Камеры не выровнены"))


if __name__ == "__main__":
    unittest.main()
