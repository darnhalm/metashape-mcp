import os
import tempfile
import unittest
from pathlib import Path

from mask_imports import MaskImports, validate_template


class FakeBridge:
    def __init__(self, photos):
        self.photos = photos
        self.calls = []

    async def call(self, method, timeout=5, **parameters):
        self.calls.append((method, parameters))
        if method == "mask_import_targets":
            keys = parameters["camera_keys"]
            items = [(key, path) for key, path in self.photos.items() if keys is None or key in keys]
            return {"chunk_key": parameters["chunk_key"], "cameras": [
                {"key": key, "label": f"IMG_{key}", "photo_path": path, "has_mask": key == 2}
                for key, path in items]}
        if method == "mask_import":
            return {"selected_cameras": len(parameters["camera_keys"]),
                    "cameras_with_mask_after": len(parameters["camera_keys"])}
        raise AssertionError(method)


class ValidateTemplateTests(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(validate_template("/m/{filename}_mask.png"), Path("/m"))
        for bad in ("m/{filename}.png", "/m/mask.png", "/{filename}/a.png",
                    "/m/{filename}_{camera}.png", "/m/{filename}.txt", ""):
            with self.assertRaises(ValueError, msg=bad):
                validate_template(bad)


class MaskImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.masks = root / "masks"
        self.masks.mkdir()
        (self.masks / "IMG_1_mask.png").write_bytes(b"one")
        (self.masks / "IMG_2_mask.png").write_bytes(b"two")
        self.template = str(self.masks / "{filename}_mask.png")
        self.bridge = FakeBridge({1: "/photos/IMG_1.jpg", 2: "/photos/IMG_2.JPG", 3: "/photos/IMG_3.jpg"})
        self.events = []
        self.imports = MaskImports(root / "service", self.bridge,
                                   lambda event, **fields: self.events.append(event))

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_preview_then_import_found_cameras_only(self):
        preview = await self.imports.prepare(4, self.template)
        self.assertEqual((preview["found_count"], preview["missing_count"]), (2, 1))
        self.assertEqual(preview["replaced_existing_masks"], 1)
        self.assertEqual(preview["missing"][0]["key"], 3)
        result = await self.imports.apply(preview["preview_id"])
        method, parameters = self.bridge.calls[-1]
        self.assertEqual(method, "mask_import")
        self.assertEqual(parameters["camera_keys"], [1, 2])
        self.assertEqual(parameters["operation"], "replacement")
        self.assertEqual(result["cameras_with_mask_after"], 2)
        self.assertEqual(self.events, ["mask_import_prepared", "mask_import_completed"])

    async def test_changed_mask_file_blocks_import(self):
        preview = await self.imports.prepare(4, self.template, camera_keys=[1])
        path = self.masks / "IMG_1_mask.png"
        path.write_bytes(b"changed")
        os.utime(path, ns=(1, 1))
        with self.assertRaises(ValueError):
            await self.imports.apply(preview["preview_id"])
        self.assertNotIn("mask_import", [call[0] for call in self.bridge.calls])

    async def test_changed_camera_photo_blocks_import(self):
        preview = await self.imports.prepare(4, self.template, camera_keys=[1])
        self.bridge.photos[1] = "/other/IMG_1.jpg"
        with self.assertRaises(ValueError):
            await self.imports.apply(preview["preview_id"])

    async def test_nothing_found_blocks_import(self):
        preview = await self.imports.prepare(4, self.template, camera_keys=[3])
        with self.assertRaises(ValueError):
            await self.imports.apply(preview["preview_id"])

    async def test_parameter_checks(self):
        with self.assertRaises(ValueError):
            await self.imports.prepare(4, self.template, operation="xor")
        with self.assertRaises(ValueError):
            await self.imports.prepare(4, self.template, camera_keys=[1, 1])
        with self.assertRaises(ValueError):
            await self.imports.prepare(4, str(self.masks / "nope" / "{filename}.png"))


if __name__ == "__main__":
    unittest.main()
