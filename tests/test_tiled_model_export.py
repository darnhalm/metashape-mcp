import unittest

from tiled_model_export import tiled_model_export_spec


class TiledModelExportTests(unittest.TestCase):
    def test_cesium_uses_3tz_and_routes_cesium_options(self):
        result = tiled_model_export_spec(
            "cesium", "1.1", False, False, 8, 3)
        self.assertEqual(result["filename"], "tiled-model.3tz")
        self.assertEqual(result["arguments"]["tileset_version"], "1.1")
        self.assertEqual(result["arguments"]["screen_space_error"], 8.0)
        self.assertFalse(result["arguments"]["model_compression"])

    def test_non_cesium_formats_do_not_receive_cesium_only_options(self):
        self.assertEqual(tiled_model_export_spec("slpk")["arguments"], {})
        self.assertEqual(tiled_model_export_spec("osgb")["filename"], "tiled-model.osgb")

    def test_invalid_format_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cesium"):
            tiled_model_export_spec("zip")

    def test_invalid_tileset_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "tileset_version"):
            tiled_model_export_spec("cesium", "2.0")

    def test_invalid_numeric_options_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "screen_space_error"):
            tiled_model_export_spec("cesium", screen_space_error=0)
        with self.assertRaisesRegex(ValueError, "folder_depth"):
            tiled_model_export_spec("cesium", folder_depth=True)


if __name__ == "__main__":
    unittest.main()
