import tempfile
import unittest
from pathlib import Path

from reference_data import ReferenceImports


class FakeBridge:
    def __init__(self):
        self.calls = []

    async def call(self, method, **parameters):
        self.calls.append((method, parameters))
        if method == "reference_targets":
            return {"camera_labels": ["IMG_001", "IMG_002"], "marker_labels": ["GCP_1"]}
        if method == "reference_import":
            return {"imported": True, "chunk_key": parameters["chunk_key"]}
        raise AssertionError(method)


class ReferenceImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bridge = FakeBridge()
        self.events = []
        self.imports = ReferenceImports(
            self.root, self.bridge, lambda event, **fields: self.events.append((event, fields)))

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_preview_matches_targets_and_imports_unchanged_file(self):
        path = self.root / "reference.csv"
        path.write_text("label,x,y,z\nIMG_001,1,2,3\nGCP_1,4,5,6\nUNKNOWN,7,8,9\n")
        preview = await self.imports.prepare(
            4, str(path), "nxyz", ",", 1, 4326, "markers", True)
        self.assertEqual(preview["valid_rows"], 3)
        self.assertEqual(preview["matched_cameras"], 1)
        self.assertEqual(preview["matched_markers"], 1)
        self.assertEqual(preview["unmatched_labels"], ["IMG_001", "UNKNOWN"])
        result = await self.imports.apply(preview["preview_id"])
        self.assertTrue(result["imported"])

    async def test_duplicate_or_invalid_rows_block_import(self):
        path = self.root / "bad.csv"
        path.write_text(
            "IMG_001,1,2,3\nIMG_001,x,2,3\nIMG_001,4,5,6\nIMG_002,nan,2,3\n")
        preview = await self.imports.prepare(
            1, str(path), "nxyz", ",", 0, None, "cameras", False)
        self.assertEqual(preview["invalid_row_count"], 2)
        self.assertEqual(preview["duplicate_count"], 1)
        with self.assertRaisesRegex(ValueError, "Импорт заблокирован"):
            await self.imports.apply(preview["preview_id"])

    async def test_changed_file_requires_new_preview(self):
        path = self.root / "reference.csv"
        path.write_text("IMG_001,1,2,3\n")
        preview = await self.imports.prepare(
            1, str(path), "nxyz", ",", 0, None, "cameras", False)
        path.write_text("IMG_002,1,2,3\n")
        with self.assertRaisesRegex(ValueError, "изменился"):
            await self.imports.apply(preview["preview_id"])


if __name__ == "__main__":
    unittest.main()
