"""Validation for copy-based camera reference loading from EXIF/XMP."""

import math


def _positive_vector(name, value):
    if (not isinstance(value, list) or len(value) != 3
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) or item <= 0 for item in value)):
        raise ValueError(f"{name} должен содержать три положительных числа.")
    return [float(item) for item in value]


def validate_reference_exif_plan(epsg, fallback_accuracy_m, load_rotation=False,
                                 rotation_accuracy_deg=None, load_accuracy=True,
                                 replace_existing=False, enable_locations=True,
                                 enable_rotations=False, update_transform=False):
    if epsg != 4326:
        raise ValueError("EXIF GPS хранит широту/долготу WGS 84: явно укажите epsg=4326.")
    fallback_accuracy_m = _positive_vector("fallback_accuracy_m", fallback_accuracy_m)
    for name, value in (
            ("load_rotation", load_rotation), ("load_accuracy", load_accuracy),
            ("replace_existing", replace_existing), ("enable_locations", enable_locations),
            ("enable_rotations", enable_rotations), ("update_transform", update_transform)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} должен быть true или false.")
    if load_rotation:
        rotation_accuracy_deg = _positive_vector(
            "rotation_accuracy_deg", rotation_accuracy_deg)
    elif rotation_accuracy_deg is not None or enable_rotations:
        raise ValueError("rotation_accuracy_deg/enable_rotations допустимы только при "
                         "load_rotation=true.")
    return {
        "epsg": 4326,
        "fallback_accuracy_m": fallback_accuracy_m,
        "load_rotation": load_rotation,
        "rotation_accuracy_deg": rotation_accuracy_deg,
        "load_accuracy": load_accuracy,
        "replace_existing": replace_existing,
        "enable_locations": enable_locations,
        "enable_rotations": enable_rotations,
        "update_transform": update_transform,
    }
