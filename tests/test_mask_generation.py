import os
import tempfile
import unittest
import zlib
from pathlib import Path

from mask_imports import MaskGeneration, png_has_alpha


def png(path, color_type, trns=False):
    def chunk(kind, data):
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")
    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, color_type, 0, 0, 0])
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if trns:
        data += chunk(b"tRNS", b"\x00\x00")
    data += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + chunk(b"IEND", b"")
    Path(path).write_bytes(data)


class FakeBridge:
    def __init__(self, photos):
        self.photos = photos
        self.calls = []

    async def call(self, method, timeout=5, **parameters):
        self.calls.append((method, parameters))
        if method == "mask_import_targets":
            keys = parameters["camera_keys"]
            return {"cameras": [
                {"key": key, "label": Path(path).stem, "photo_path": path, "has_mask": False}
                for key, path in self.photos.items() if keys is None or key in keys]}
        if method == "mask_generate":
            return {"cameras_with_mask_after": len(parameters["camera_keys"])}
        raise AssertionError(method)


class PngHeaderTests(unittest.TestCase):
    def test_alpha_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            png(root / "rgba.png", 6)
            png(root / "rgb.png", 2)
            png(root / "trns.png", 2, trns=True)
            (root / "bad.png").write_bytes(b"not a png")
            self.assertTrue(png_has_alpha(root / "rgba.png"))
            self.assertFalse(png_has_alpha(root / "rgb.png"))
            self.assertTrue(png_has_alpha(root / "trns.png"))
            self.assertIsNone(png_has_alpha(root / "bad.png"))


class MaskGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        photos = root / "photos"
        photos.mkdir()
        png(photos / "A.png", 6)
        png(photos / "B.png", 2)
        (photos / "C.jpg").write_bytes(b"jpeg")
        (photos / "D.tif").write_bytes(b"tiff")
        self.backgrounds = root / "backgrounds"
        self.backgrounds.mkdir()
        (self.backgrounds / "A_bg.png").write_bytes(b"bg")
        self.bridge = FakeBridge({1: str(photos / "A.png"), 2: str(photos / "B.png"),
                                  3: str(photos / "C.jpg"), 4: str(photos / "D.tif"),
                                  5: str(photos / "missing.png")})
        self.generation = MaskGeneration(root / "service", self.bridge, lambda *a, **k: None)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_alpha_selects_usable_photos(self):
        preview = await self.generation.prepare(1, "alpha")
        self.assertEqual(preview["found_count"], 2)
        self.assertEqual(preview["alpha_unknown"], 1)
        reasons = {item["key"]: item["reason"] for item in preview["missing"]}
        self.assertEqual(reasons, {2: "no_alpha_channel", 3: "jpeg_has_no_alpha", 5: "photo_not_found"})
        await self.generation.apply(preview["preview_id"])
        method, parameters = self.bridge.calls[-1]
        self.assertEqual((method, parameters["camera_keys"], parameters["mode"]), ("mask_generate", [1, 4], "alpha"))
        self.assertIsNone(parameters["template"])

    async def test_background_requires_template_and_files(self):
        with self.assertRaises(ValueError):
            await self.generation.prepare(1, "background")
        with self.assertRaises(ValueError):
            await self.generation.prepare(1, "alpha", background_template=str(self.backgrounds / "{filename}_bg.png"))
        preview = await self.generation.prepare(
            1, "background", tolerance=30, background_template=str(self.backgrounds / "{filename}_bg.png"))
        self.assertEqual(preview["found_count"], 1)
        await self.generation.apply(preview["preview_id"])
        parameters = self.bridge.calls[-1][1]
        self.assertEqual((parameters["camera_keys"], parameters["tolerance"]), ([1], 30))

    async def test_changed_background_blocks(self):
        template = str(self.backgrounds / "{filename}_bg.png")
        preview = await self.generation.prepare(1, "background", background_template=template)
        path = self.backgrounds / "A_bg.png"
        path.write_bytes(b"changed background")
        os.utime(path, ns=(1, 1))
        with self.assertRaises(ValueError):
            await self.generation.apply(preview["preview_id"])
        self.assertNotIn("mask_generate", [call[0] for call in self.bridge.calls])

    async def test_parameter_checks(self):
        for kwargs in ({"mode": "ai"}, {"mode": "alpha", "tolerance": 300},
                       {"mode": "alpha", "operation": "xor"}):
            with self.assertRaises(ValueError):
                await self.generation.prepare(1, **kwargs)


if __name__ == "__main__":
    unittest.main()
