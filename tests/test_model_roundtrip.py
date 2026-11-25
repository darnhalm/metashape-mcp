import json
import tempfile
import unittest
from pathlib import Path

from model_roundtrip import (SCHEMA, bounds_from_points, compare_bounds,
                             file_fingerprint, inspect_reimport, matrix_max_delta)


class ModelRoundtripTests(unittest.TestCase):
    def test_bounds_detect_translation_scale_and_axis_change(self):
        source = bounds_from_points([(0, 0, 0), (2, 4, 8)])
        exact = compare_bounds(source, bounds_from_points([(0, 0, 0), (2, 4, 8)]), 1e-6)
        moved = compare_bounds(source, bounds_from_points([(1, 0, 0), (3, 4, 8)]), 1e-3)
        swapped = compare_bounds(source, bounds_from_points([(0, 0, 0), (8, 4, 2)]), 1e-3)
        self.assertTrue(exact["coordinate_match"])
        self.assertFalse(moved["coordinate_match"])
        self.assertFalse(swapped["coordinate_match"])

    def test_matrix_delta(self):
        self.assertEqual(0, matrix_max_delta([1, 2], [1, 2]))
        self.assertAlmostEqual(0.25, matrix_max_delta([1, 2], [1.25, 2]))
        self.assertIsNone(matrix_max_delta(None, [1]))

    def test_manifest_binds_name_format_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.obj"
            model.write_text("v 0 0 0\n", encoding="utf-8")
            manifest = {
                "schema": SCHEMA,
                "source": {"chunk_key": 1, "local_bounds": bounds_from_points([(0, 0, 0), (1, 1, 1)])},
                "export": {"format": "obj", "coordinate_space": "chunk_internal",
                           "epsg": None, "shift": [0, 0, 0]},
                "model_file": {"name": model.name, "fingerprint": file_fingerprint(model)},
            }
            path = root / "model-coordinate-manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            inspected = inspect_reimport(model)
            self.assertFalse(inspected["modified_since_export"])
            model.write_text("v 1 0 0\n", encoding="utf-8")
            self.assertTrue(inspect_reimport(model)["modified_since_export"])

    def test_invalid_manifest_and_tolerance_are_rejected(self):
        bounds = bounds_from_points([(0, 0, 0), (1, 1, 1)])
        with self.assertRaises(ValueError):
            compare_bounds(bounds, bounds, 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.xyz"
            model.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_reimport(model)


if __name__ == "__main__":
    unittest.main()
