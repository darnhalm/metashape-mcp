"""Validation for copy-based transformation of reference coordinates."""


def _epsg(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999999:
        raise ValueError(f"{name} должен быть целым кодом EPSG от 1 до 999999.")
    return value


def validate_reference_crs_transform(source_epsg, target_epsg, transform_cameras=True,
                                     transform_markers=True, set_chunk_crs=False,
                                     update_transform=False):
    source_epsg = _epsg("source_epsg", source_epsg)
    target_epsg = _epsg("target_epsg", target_epsg)
    if source_epsg == target_epsg:
        raise ValueError("Исходная и целевая системы координат должны различаться.")
    for name, value in (("transform_cameras", transform_cameras),
                        ("transform_markers", transform_markers),
                        ("set_chunk_crs", set_chunk_crs),
                        ("update_transform", update_transform)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} должен быть true или false.")
    if not transform_cameras and not transform_markers:
        raise ValueError("Выберите хотя бы один класс: камеры или маркеры.")
    return {
        "source_epsg": source_epsg, "target_epsg": target_epsg,
        "transform_cameras": transform_cameras, "transform_markers": transform_markers,
        "set_chunk_crs": set_chunk_crs, "update_transform": update_transform,
    }
