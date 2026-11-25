import os
import tempfile
import unittest
from pathlib import Path

from cloud_files import write_las, write_ply
from point_cloud_imports import PointCloudImports, inspect_file

POINTS = [(i * 0.01, (i % 7) * 0.02, (i % 5) * 0.03, i % 256, 10, 200) for i in range(250)]


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.existing = [{"key": 0, "label": "Point Cloud", "point_count": 9}]

    async def call(self, method, timeout=5, **parameters):
        self.calls.append((method, parameters))
        if method == "point_cloud_import_target":
            return {"point_clouds": self.existing, "active_point_cloud_key": 0,
                    "coordinate_system": None, "epsg_name": "WGS 84" if parameters["epsg"] else None}
        if method == "point_cloud_import":
            return {"point_cloud_key": 1, "imported_point_count": 250}
        raise AssertionError(method)


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_headers(self):
        write_las(self.root / "a.las", POINTS)
        write_ply(self.root / "a.ply", POINTS)
        (self.root / "a.e57").write_bytes(b"ASTM-E57")
        self.assertEqual(inspect_file(self.root / "a.las")["header_point_count"], 250)
        self.assertEqual(inspect_file(self.root / "a.las")["las_version"], "1.2")
        self.assertEqual(inspect_file(self.root / "a.ply")["header_point_count"], 250)
        self.assertIsNone(inspect_file(self.root / "a.e57")["header_point_count"])

    def test_rejections(self):
        (self.root / "bad.las").write_bytes(b"not las" * 50)
        (self.root / "a.xyz").write_text("0 0 0\n")
        for name in ("bad.las", "a.xyz", "missing.las"):
            with self.assertRaises(ValueError, msg=name):
                inspect_file(self.root / name)
        with self.assertRaises(ValueError):
            inspect_file("relative.las")


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.las = self.root / "cloud.las"
        write_las(self.las, POINTS)
        self.bridge = FakeBridge()
        self.imports = PointCloudImports(self.root / "service", self.bridge, lambda *a, **k: None)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_preview_and_import(self):
        preview = await self.imports.prepare(3, str(self.las), epsg=4326)
        self.assertEqual(preview["header_point_count"], 250)
        self.assertEqual(preview["epsg_name"], "WGS 84")
        result = await self.imports.apply(preview["preview_id"])
        method, parameters = self.bridge.calls[-1]
        self.assertEqual((method, parameters["format"], parameters["expected_existing"]),
                         ("point_cloud_import", "las", [0]))
        self.assertTrue(result["point_count_matches_header"])

    async def test_changed_file_blocks(self):
        preview = await self.imports.prepare(3, str(self.las))
        write_las(self.las, POINTS[:10])
        os.utime(self.las, ns=(1, 1))
        with self.assertRaises(ValueError):
            await self.imports.apply(preview["preview_id"])
        self.assertNotIn("point_cloud_import", [call[0] for call in self.bridge.calls])

    async def test_bad_epsg(self):
        with self.assertRaises(ValueError):
            await self.imports.prepare(3, str(self.las), epsg=0)


if __name__ == "__main__":
    unittest.main()
