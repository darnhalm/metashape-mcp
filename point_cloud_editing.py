"""Validation for reversible point-cloud cleanup jobs."""

import math


def _classes(values, required):
    if values is None:
        values = []
    if (not isinstance(values, list)
            or any(isinstance(value, bool) or not isinstance(value, int)
                   or not 0 <= value <= 127 for value in values)):
        raise ValueError("point_classes должен содержать целые классы от 0 до 127.")
    if len(values) != len(set(values)):
        raise ValueError("point_classes содержит повторы.")
    if required and not values:
        raise ValueError("Для selector='classes' укажите хотя бы один point_classes.")
    return sorted(values)


def validate_point_cloud_cleanup(point_cloud_key, selector, point_classes=None,
                                 min_confidence=None, max_confidence=None,
                                 max_selected_fraction=0.30):
    if isinstance(point_cloud_key, bool) or not isinstance(point_cloud_key, int):
        raise ValueError("point_cloud_key должен быть целым ключом облака.")
    if selector not in ("classes", "confidence"):
        raise ValueError("selector должен быть 'classes' или 'confidence'.")
    point_classes = _classes(point_classes, selector == "classes")
    if (isinstance(max_selected_fraction, bool)
            or not isinstance(max_selected_fraction, (int, float))
            or not math.isfinite(float(max_selected_fraction))
            or not 0 < float(max_selected_fraction) <= 1):
        raise ValueError("max_selected_fraction должен быть больше 0 и не больше 1.")
    if selector == "confidence":
        for name, value in (("min_confidence", min_confidence),
                            ("max_confidence", max_confidence)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                raise ValueError(f"{name} должен быть целым числом от 0 до 255.")
        if min_confidence > max_confidence:
            raise ValueError("min_confidence не может быть больше max_confidence.")
    elif min_confidence is not None or max_confidence is not None:
        raise ValueError("Диапазон confidence допустим только для selector='confidence'.")
    return {
        "point_cloud_key": point_cloud_key, "selector": selector,
        "point_classes": point_classes, "min_confidence": min_confidence,
        "max_confidence": max_confidence,
        "max_selected_fraction": float(max_selected_fraction),
    }
