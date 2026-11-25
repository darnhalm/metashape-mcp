"""Pure helpers for coordinate-safe model export and reimport."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


SCHEMA = "metashape-mcp-model-roundtrip/1"
MANIFEST_NAME = "model-coordinate-manifest.json"
FORMATS = {
    ".obj": "obj",
    ".ply": "ply",
    ".stl": "stl",
    ".fbx": "fbx",
    ".glb": "glb",
}


def file_fingerprint(path: str | Path) -> dict[str, object]:
    path = Path(path)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest()}


def bounds_from_points(points) -> dict[str, list[float]]:
    rows = [[float(value) for value in point] for point in points]
    if not rows or any(len(row) != 3 for row in rows):
        raise ValueError("Для границ модели нужны трёхмерные вершины.")
    minimum = [min(row[index] for row in rows) for index in range(3)]
    maximum = [max(row[index] for row in rows) for index in range(3)]
    return {
        "min": minimum,
        "max": maximum,
        "center": [(minimum[index] + maximum[index]) / 2 for index in range(3)],
        "size": [maximum[index] - minimum[index] for index in range(3)],
    }


def compare_bounds(source: dict[str, object], imported: dict[str, object],
                   tolerance_ratio: float) -> dict[str, object]:
    if not 0 < tolerance_ratio <= 0.1:
        raise ValueError("coordinate_tolerance_ratio должен быть больше 0 и не больше 0.1.")
    source_size = [float(value) for value in source["size"]]
    imported_size = [float(value) for value in imported["size"]]
    diagonal = math.sqrt(sum(value * value for value in source_size))
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise ValueError("У исходной модели нулевой или некорректный размер.")
    center_delta = [float(imported["center"][index]) - float(source["center"][index])
                    for index in range(3)]
    size_delta = [imported_size[index] - source_size[index] for index in range(3)]
    center_shift = math.sqrt(sum(value * value for value in center_delta))
    size_change = math.sqrt(sum(value * value for value in size_delta))
    center_ratio = center_shift / diagonal
    size_ratio = size_change / diagonal
    return {
        "source_bounds": source,
        "imported_bounds": imported,
        "center_delta": center_delta,
        "size_delta": size_delta,
        "source_diagonal": diagonal,
        "center_shift_ratio": center_ratio,
        "size_change_ratio": size_ratio,
        "tolerance_ratio": tolerance_ratio,
        "coordinate_match": center_ratio <= tolerance_ratio and size_ratio <= tolerance_ratio,
        "note": ("Сравнение границ надёжно выявляет обычный сдвиг, масштаб и перестановку осей; "
                 "при сильно изменённой геометрии оно не является доказательством совпадения каждой вершины."),
    }


def matrix_max_delta(first, second) -> float | None:
    if first is None or second is None:
        return 0.0 if first is None and second is None else None
    a = [float(value) for value in first]
    b = [float(value) for value in second]
    if len(a) != len(b):
        return None
    return max((abs(left - right) for left, right in zip(a, b)), default=0.0)


def load_manifest(path: str | Path) -> dict[str, object]:
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Не найден абсолютный путь к model-coordinate-manifest.json.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Не удалось прочитать манифест реимпорта: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"Неподдерживаемая схема манифеста; ожидается {SCHEMA}.")
    required = {"source", "export", "model_file"}
    if not required.issubset(value):
        raise ValueError(f"В манифесте отсутствуют разделы: {sorted(required - set(value))}.")
    source = value["source"]
    export = value["export"]
    if not isinstance(source, dict) or not isinstance(export, dict):
        raise ValueError("Разделы source/export манифеста должны быть объектами.")
    if export.get("coordinate_space") not in ("chunk_crs", "chunk_internal", "epsg"):
        raise ValueError("Неизвестное координатное пространство в манифесте.")
    if export.get("coordinate_space") == "epsg" and not isinstance(export.get("epsg"), int):
        raise ValueError("Для coordinate_space=epsg в манифесте нужен числовой EPSG.")
    shift = export.get("shift")
    if (not isinstance(shift, list) or len(shift) != 3
            or any(not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in shift)):
        raise ValueError("В манифесте нужен конечный трёхмерный export.shift.")
    return value


def inspect_reimport(path: str | Path, manifest_path: str | Path | None = None) -> dict[str, object]:
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Укажите абсолютный путь к существующему файлу модели.")
    kind = FORMATS.get(path.suffix.lower())
    if kind is None:
        raise ValueError("Для проверяемого реимпорта поддерживаются OBJ, PLY, STL, FBX и GLB.")
    manifest_path = (Path(manifest_path).expanduser() if manifest_path is not None
                     else path.parent / MANIFEST_NAME)
    manifest = load_manifest(manifest_path)
    expected_name = manifest["model_file"].get("name")
    if expected_name and expected_name != path.name:
        raise ValueError(f"Манифест относится к файлу {expected_name}, а выбран {path.name}.")
    if manifest["export"].get("format") != kind:
        raise ValueError("Формат модели не совпадает с форматом в манифесте.")
    current = file_fingerprint(path)
    original = manifest["model_file"].get("fingerprint") or {}
    modified = (original.get("size") != current["size"]
                or original.get("sha256") != current["sha256"])
    return {
        "path": str(path.resolve()),
        "format": kind,
        "manifest_path": str(manifest_path.resolve()),
        "manifest": manifest,
        "fingerprint": current,
        "modified_since_export": modified,
    }
