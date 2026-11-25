"""Read-only mask inspection helpers for Metashape cameras.

The functions only read ``camera.mask``; they never call ``generateMasks``,
assign masks or replace assets. Metashape objects are accessed by duck typing,
so the logic can be tested without Metashape.
"""

MAX_COVERAGE_PAGE = 20


def masked_fraction(data, width, height, channels=1):
    """Return the share of mask pixels equal to zero (excluded by the mask).

    Only 8-bit data is supported. The first channel decides for multi-channel
    images. Returns ``None`` when the buffer does not match the declared size.
    """
    pixels = int(width) * int(height)
    channels = int(channels or 1)
    if pixels <= 0 or len(data) != pixels * channels:
        return None
    if channels > 1:
        data = bytes(data)[::channels]
    return bytes(data).count(0) / pixels


def mask_coverage(mask):
    """Read pixel coverage of one Metashape mask. Returns a dict or an error note."""
    try:
        image = mask.image()
    except Exception as error:  # Metashape raises RuntimeError on unreadable masks
        return {"coverage_error": str(error)}
    width, height = int(image.width), int(image.height)
    channels = int(getattr(image, "cn", 1) or 1)
    data_type = str(getattr(image, "data_type", "U8"))
    result = {"mask_width": width, "mask_height": height}
    if data_type != "U8":
        result["coverage_error"] = f"Неподдерживаемый тип данных маски: {data_type}"
        return result
    data = image.tostring()
    fraction = masked_fraction(data, width, height, channels)
    if fraction is None:
        result["coverage_error"] = "Размер данных маски не совпадает с размером изображения."
    else:
        result["masked_pixel_fraction"] = round(fraction, 6)
        result["masked_by_row_band"] = row_bands(data, width, height, channels)
    return result


def row_bands(data, width, height, channels=1, bands=10):
    """Masked share in horizontal bands from top to bottom — shows where the mask is."""
    data = bytes(data)
    row = width * channels
    result = []
    for band in range(bands):
        top, bottom = height * band // bands, height * (band + 1) // bands
        chunk = data[top * row:bottom * row][::channels]
        result.append(round(chunk.count(0) / max(1, len(chunk)), 3))
    return result


def mask_summary(chunk, offset, limit, include_coverage, is_regular):
    """Summarize masks of regular cameras in the chunk's current frame."""
    if offset < 0 or limit < 1:
        raise ValueError("Неверные параметры постраничного вывода.")
    if include_coverage and limit > MAX_COVERAGE_PAGE:
        raise ValueError(
            f"С include_coverage=true limit не может превышать {MAX_COVERAGE_PAGE}: "
            "чтение пикселей масок выполняется в основном потоке Metashape.")
    cameras = [camera for camera in chunk.cameras if is_regular(camera)]
    has_mask = [camera.mask is not None for camera in cameras]
    stop = min(offset + limit, len(cameras))
    rows = []
    for index in range(offset, stop):
        camera = cameras[index]
        row = {"key": camera.key, "label": camera.label, "enabled": camera.enabled,
               "has_mask": has_mask[index]}
        if include_coverage and has_mask[index]:
            row.update(mask_coverage(camera.mask))
        rows.append(row)
    return {
        "chunk_key": chunk.key,
        "camera_count": len(cameras),
        "cameras_with_mask": sum(has_mask),
        "cameras_without_mask": len(cameras) - sum(has_mask),
        "enabled_cameras_without_mask": sum(
            camera.enabled and not masked for camera, masked in zip(cameras, has_mask)),
        "coverage_included": bool(include_coverage),
        "cameras": rows,
        "total": len(cameras),
        "next_offset": stop if stop < len(cameras) else None,
    }
