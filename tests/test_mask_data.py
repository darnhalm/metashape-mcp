import unittest
from types import SimpleNamespace

from mask_data import MAX_COVERAGE_PAGE, mask_summary, masked_fraction


class FakeImage:
    def __init__(self, data, width, height, cn=1, data_type="U8"):
        self.data, self.width, self.height, self.cn, self.data_type = data, width, height, cn, data_type

    def tostring(self):
        return self.data


class FakeMask:
    def __init__(self, image=None, error=None):
        self._image, self._error = image, error

    def image(self):
        if self._error:
            raise RuntimeError(self._error)
        return self._image


def camera(key, mask=None, enabled=True, regular=True):
    return SimpleNamespace(key=key, label=f"IMG_{key}", enabled=enabled, mask=mask, regular=regular)


def regular(item):
    return item.regular


class MaskedFractionTests(unittest.TestCase):
    def test_counts_zero_pixels(self):
        self.assertEqual(masked_fraction(bytes([0, 255, 0, 255]), 2, 2), 0.5)

    def test_multichannel_uses_first_channel(self):
        data = bytes([0, 9, 9, 255, 0, 0])
        self.assertEqual(masked_fraction(data, 2, 1, 3), 0.5)

    def test_size_mismatch(self):
        self.assertIsNone(masked_fraction(b"\x00", 2, 2))


class MaskSummaryTests(unittest.TestCase):
    def setUp(self):
        full = FakeMask(FakeImage(bytes([0, 0, 255, 255]), 2, 2))
        self.chunk = SimpleNamespace(key=7, cameras=[
            camera(1, full), camera(2), camera(3, enabled=False),
            camera(4, FakeMask(error="cannot read")), camera(5, regular=False)])

    def test_counts_and_pagination(self):
        result = mask_summary(self.chunk, 0, 2, False, regular)
        self.assertEqual(result["camera_count"], 4)
        self.assertEqual(result["cameras_with_mask"], 2)
        self.assertEqual(result["enabled_cameras_without_mask"], 1)
        self.assertEqual([row["key"] for row in result["cameras"]], [1, 2])
        self.assertEqual(result["next_offset"], 2)
        self.assertNotIn("masked_pixel_fraction", result["cameras"][0])
        last = mask_summary(self.chunk, 2, 2, False, regular)
        self.assertIsNone(last["next_offset"])

    def test_coverage(self):
        rows = mask_summary(self.chunk, 0, 4, True, regular)["cameras"]
        self.assertEqual(rows[0]["masked_pixel_fraction"], 0.5)
        self.assertNotIn("masked_pixel_fraction", rows[1])
        self.assertEqual(rows[3]["coverage_error"], "cannot read")

    def test_coverage_page_cap(self):
        with self.assertRaises(ValueError):
            mask_summary(self.chunk, 0, MAX_COVERAGE_PAGE + 1, True, regular)


if __name__ == "__main__":
    unittest.main()


class RowBandTests(unittest.TestCase):
    def test_top_half_masked(self):
        from mask_data import row_bands
        data = bytes([0] * 20 + [255] * 20)  # 4x10 image, top 5 rows black
        self.assertEqual(row_bands(data, 4, 10, 1, bands=2), [1.0, 0.0])
