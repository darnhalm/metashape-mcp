import unittest

from autopilot import required_inputs, validate_steps


def steps(*items):
    return validate_steps([{"task": task, "parameters": parameters} for task, parameters in items])


class AutopilotSourcesTest(unittest.TestCase):
    def test_default_inputs(self):
        self.assertEqual(required_inputs(steps(("BuildModel", {}))), ["depth_maps"])
        self.assertEqual(required_inputs(steps(("BuildTiledModel", {}))), ["depth_maps"])

    def test_source_data_changes_what_is_copied(self):
        self.assertEqual(required_inputs(steps(("BuildModel", {"source_data": "PointCloudData"}))),
                         ["point_cloud"])
        self.assertEqual(required_inputs(steps(("BuildTiledModel", {"source_data": "ModelData"}))),
                         ["model"])
        # colour sources add to, not replace, the model the task works on
        self.assertEqual(required_inputs(steps(("ColorizeModel", {"source_data": "ImagesData"}))), ["model"])
        self.assertEqual(required_inputs(steps(("BuildTexture", {"source_data": "ImagesData"}))), ["model"])
        self.assertEqual(required_inputs(steps(("CalibrateColors", {"source_data": "OrthomosaicData"}))),
                         ["orthomosaic"])
        self.assertEqual(required_inputs(steps(("BuildOrthomosaic", {"surface_data": "ModelData"}))),
                         ["model"])

    def test_produced_input_is_not_copied(self):
        chain = steps(("BuildDepthMaps", {}), ("BuildPointCloud", {}),
                      ("BuildModel", {"source_data": "PointCloudData"}), ("RefineModel", {}))
        self.assertEqual(required_inputs(chain), [])

    def test_unknown_source_and_file_parameters_are_refused(self):
        with self.assertRaises(ValueError):
            required_inputs(steps(("BuildModel", {"source_data": "LaserScansData"})))
        with self.assertRaises(ValueError):
            steps(("RemoveLighting", {"ambient_occlusion_path": "/tmp/ao.png"}))
        with self.assertRaises(ValueError):
            steps(("ReduceOverlap", {}))


if __name__ == "__main__":
    unittest.main()
