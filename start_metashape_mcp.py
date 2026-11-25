"""Start the MCP service in the current Metashape Professional GUI."""

import base64
import hashlib
import json
import math
import os
import runpy
import shutil
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import Metashape

HERE = Path(__file__).resolve().parent
# Forget local helper modules by name as well as by path.  The name check matters
# when a user moves or upgrades the MCP folder: Python would otherwise reuse a
# module cached from the previous directory before the new directory enters
# sys.path, producing a mixed-version host service.
_local_module_names = {path.stem for path in HERE.glob("*.py")}
for _name, _module in list(sys.modules.items()):
    _file = getattr(_module, "__file__", None)
    if (_name != "__main__"
            and (_name.partition(".")[0] in _local_module_names
                 or _file and Path(_file).resolve().parent == HERE)):
        del sys.modules[_name]
sys.path.insert(0, str(HERE))
try:
    from service_monitor import DEFAULT_URL, QtCore, QtWidgets, show_service_monitor, start_sdk_process
    from camera_alignment import CameraAlignment
    from mcp_settings import load_settings, save_settings
    from mask_data import mask_summary
    from path_policy import describe_roots, resolve_scoped
    from restore_points import RestorePoints
    from measurements import measurement_basis
    from progress_tracker import TRACKER
    from tiled_model_export import tiled_model_export_spec
    from diagnostics import numeric_summary, path_diagnostics, unaligned_runs
    from model_roundtrip import (MANIFEST_NAME, SCHEMA, bounds_from_points,
                                 file_fingerprint)
finally:
    sys.path.pop(0)


# Long host calls outside job stages whose Metashape progress is tracked (get_service_status.operation).
TRACKED_HOST_METHODS = {"asset_export", "point_cloud_import", "mask_import", "mask_generate",
                        "chunk_alignment_apply", "chunk_merge_apply"}


def find_chunk(doc, key):
    chunk = next((c for c in doc.chunks if c.key == key), None) if doc is not None else None
    if chunk is None:
        raise ValueError("Выбранный чанк отсутствует в открытом проекте.")
    return chunk


def project_path(value, existing):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Укажите абсолютный путь к проекту.")
    path = path.resolve()
    if path.suffix.lower() not in (".psx", ".psz"):
        raise ValueError("Проект должен иметь расширение .psx или .psz.")
    if existing:
        if not path.is_file():
            raise ValueError("Файл проекта не найден.")
        lock = path.with_suffix(".files") / "lock" if path.suffix.lower() == ".psx" else None
        if lock is not None and lock.is_file():
            try:
                owner = lock.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                owner = ""
            raise ValueError(
                f"Проект заблокирован ({owner or 'владелец неизвестен'}): он открыт в другом окне "
                f"Metashape или остался устаревший файл {lock}. Закройте проект там или удалите "
                "устаревший lock вручную; иначе Metashape откроет его только для чтения.")
    else:
        companion = path.with_suffix(".files") if path.suffix.lower() == ".psx" else None
        if path.exists() or companion is not None and companion.exists():
            raise ValueError("Проект или его каталог данных уже существует.")
        if not path.parent.is_dir():
            raise ValueError("Папка для проекта не существует.")
    return path


def ensure_writable(doc):
    if doc.read_only:
        raise ValueError("Проект открыт только для чтения.")


def ensure_replaceable(doc):
    if doc.modified:
        raise ValueError("В текущем проекте есть несохранённые изменения. Сначала сохраните его.")


def project_summary(doc):
    chunk = doc.chunk
    chunks = len(doc.chunks)
    if doc.modified:
        state = "modified"
    elif doc.path:
        state = "saved"
    else:
        state = "unsaved" if chunks else "empty"
    return {
        "state": state, "path": doc.path or None,
        "name": Path(doc.path).name if doc.path else None,
        "read_only": bool(doc.read_only), "modified": bool(doc.modified), "chunk_count": chunks,
        "active_chunk": {
            "key": chunk.key, "label": chunk.label, "camera_count": len(chunk.cameras),
            "marker_count": len(chunk.markers),
        } if chunk is not None else None,
    }


def page(items, request, field, describe):
    start = request["offset"]
    stop = min(start + request["limit"], len(items))
    return {field: [describe(item) for item in items[start:stop]], "total": len(items),
            "next_offset": stop if stop < len(items) else None}


def camera_summary(camera):
    regular = camera.type == Metashape.Camera.Type.Regular
    return {"key": camera.key, "label": camera.label, "type": str(camera.type).split(".")[-1],
            "enabled": camera.enabled, "aligned": (camera.transform is not None) if regular else None}


def vector_values(vector):
    return [float(value) for value in vector] if vector is not None else None


def sensor_summary(sensor, chunk):
    def calibration_values(calibration):
        if calibration is None:
            return None
        return {
            "type": str(calibration.type).split(".")[-1],
            "width": int(calibration.width), "height": int(calibration.height),
            "parameters": {
                name: float(getattr(calibration, name))
                for name in ("f", "cx", "cy", "b1", "b2", "k1", "k2", "k3", "k4",
                             "p1", "p2", "p3", "p4")
            },
        }

    result = {
        "key": sensor.key, "label": sensor.label, "type": str(sensor.type).split(".")[-1],
        "width": sensor.width, "height": sensor.height, "pixel_size_mm": vector_values(sensor.pixel_size),
        "focal_length_mm": sensor.focal_length, "fixed": sensor.fixed_calibration,
        "fixed_params": list(sensor.fixed_params),
        "camera_count": sum(camera.sensor == sensor for camera in chunk.cameras),
        "adjusted_calibration": calibration_values(sensor.calibration),
        "initial_calibration": calibration_values(sensor.user_calib),
    }
    # Kept for compatibility with clients written before 0.37.
    if sensor.calibration is not None:
        result["calibration"] = result["adjusted_calibration"]["parameters"]
    return result


def marker_summary(marker, chunk):
    squared_errors = []
    for camera in marker.projections.keys():
        projection = marker.projections[camera]
        if projection is None or camera.transform is None or marker.position is None:
            continue
        projected = camera.project(marker.position)
        if projected is None:
            continue
        squared_errors.append((projected.x - projection.coord.x) ** 2
                              + (projected.y - projection.coord.y) ** 2)
    reference = marker.reference
    return {
        "key": marker.key, "label": marker.label, "enabled": marker.enabled,
        "type": str(marker.type).split(".")[-1], "position": vector_values(marker.position),
        "reference_enabled": reference.enabled, "reference_location": vector_values(reference.location),
        "projection_count": len(marker.projections.keys()),
        "reprojection_error_rms_px": math.sqrt(sum(squared_errors) / len(squared_errors))
        if squared_errors else None,
    }


def scalebar_endpoint(point):
    kind = "marker" if isinstance(point, Metashape.Marker) else "camera"
    return {"type": kind, "key": point.key, "label": point.label}


def scalebar_summary(scalebar, chunk):
    estimated = None
    point0, point1 = scalebar.point0, scalebar.point1
    if isinstance(point0, Metashape.Marker) and isinstance(point1, Metashape.Marker):
        if (point0.position is not None and point1.position is not None
                and chunk.transform.matrix is not None):
            estimated = (chunk.transform.matrix.mulp(point0.position)
                         - chunk.transform.matrix.mulp(point1.position)).norm()
    return {
        "key": scalebar.key, "label": scalebar.label,
        "point0": scalebar_endpoint(point0), "point1": scalebar_endpoint(point1),
        "reference_enabled": scalebar.reference.enabled,
        "reference_distance": scalebar.reference.distance,
        "reference_accuracy": scalebar.reference.accuracy,
        "estimated_distance": estimated,
    }


def alignment_statistics(chunk):
    regular = [camera for camera in chunk.cameras if camera.type == Metashape.Camera.Type.Regular]
    aligned = [camera for camera in regular if camera.transform is not None]
    result = {
        "chunk_key": chunk.key, "camera_count": len(regular), "enabled_cameras": sum(c.enabled for c in regular),
        "aligned_cameras": len(aligned), "unaligned_cameras": len(regular) - len(aligned),
        "sensor_count": len(chunk.sensors), "marker_count": len(chunk.markers),
        "scalebar_count": len(chunk.scalebars), "tie_point_count": 0,
        "tie_point_projection_count": 0, "reprojection_error_rms_px": None,
        "reprojection_error_max_px": None,
    }
    tie_points = chunk.tie_points
    if tie_points is None:
        return result
    points = tie_points.points
    point_ids = [-1] * len(tie_points.tracks)
    for point_id, point in enumerate(points):
        if point.valid and 0 <= point.track_id < len(point_ids):
            point_ids[point.track_id] = point_id
            result["tie_point_count"] += 1
    squared_sum = 0.0
    maximum = 0.0
    projection_count = 0
    for camera in aligned:
        try:
            projections = tie_points.projections[camera]
        except KeyError:
            continue
        for projection in projections:
            if not 0 <= projection.track_id < len(point_ids):
                continue
            point_id = point_ids[projection.track_id]
            if point_id < 0:
                continue
            squared_error = camera.error(points[point_id].coord, projection.coord).norm() ** 2
            squared_sum += squared_error
            maximum = max(maximum, squared_error)
            projection_count += 1
    result["tie_point_projection_count"] = projection_count
    if projection_count:
        result["reprojection_error_rms_px"] = math.sqrt(squared_sum / projection_count)
        result["reprojection_error_max_px"] = math.sqrt(maximum)
    return result


def alignment_diagnostics(chunk):
    regular = [camera for camera in chunk.cameras
               if camera.type == Metashape.Camera.Type.Regular]
    matrix = chunk.transform.matrix
    scale = chunk.transform.scale
    rows = []
    for camera in regular:
        position = None
        if camera.center is not None:
            if matrix is not None:
                position = vector_values(matrix.mulp(camera.center))
            elif scale is not None and math.isfinite(scale) and scale > 0:
                position = [float(value) * float(scale) for value in camera.center]
            else:
                position = vector_values(camera.center)
        rows.append({"key": camera.key, "label": camera.label,
                     "enabled": bool(camera.enabled),
                     "aligned": camera.transform is not None,
                     "position": position})
    metric_scale = scale is not None and math.isfinite(scale) and scale > 0
    units = "meters" if metric_scale else "chunk_coordinate_units"
    return {
        "chunk_key": chunk.key,
        "units": units,
        "alignment": alignment_statistics(chunk),
        "path": path_diagnostics(rows),
        "unaligned_runs": unaligned_runs(rows),
        "reference_errors": reference_error_statistics(chunk),
        "warnings": [
            "Относительные разрывы показывают необычный шаг между соседними записями, но сами по себе не доказывают дрейф.",
            "Для независимой оценки точности нужны check points, не участвующие в привязке и оптимизации.",
        ],
    }


def find_elevation(chunk, key=None):
    elevation = chunk.elevation if key is None else next(
        (item for item in chunk.elevations if item.key == key), None)
    if elevation is None:
        raise ValueError("Нет DEM (активной или с указанным ключом).")
    return elevation


def elevation_statistics(chunk, key=None):
    elevation = find_elevation(chunk, key)
    return {
        "chunk_key": chunk.key,
        "elevation_key": elevation.key,
        "label": elevation.label,
        "active": chunk.elevation is not None and chunk.elevation.key == elevation.key,
        "coordinate_system": coordinate_system_summary(elevation.crs or chunk.crs),
        "extent": {"left": optional_float(elevation.left), "right": optional_float(elevation.right),
                   "bottom": optional_float(elevation.bottom), "top": optional_float(elevation.top)},
        "width": int(elevation.width), "height": int(elevation.height),
        "resolution_m": optional_float(elevation.resolution),
        "min_elevation": optional_float(elevation.min),
        "max_elevation": optional_float(elevation.max),
        "coverage_area_m2": optional_float(elevation.coverageArea()),
        "modified": bool(elevation.modified),
        "height_units_note": "Высоты возвращаются в вертикальных единицах CRS; обычно это метры, но проверяйте CRS/геоид.",
    }


def orthomosaic_statistics(chunk):
    rows = []
    active = chunk.orthomosaic.key if chunk.orthomosaic is not None else None
    for ortho in chunk.orthomosaics:
        rows.append({
            "key": ortho.key, "label": ortho.label, "active": ortho.key == active,
            "coordinate_system": coordinate_system_summary(getattr(ortho, "crs", None) or chunk.crs),
            "width": int(ortho.width), "height": int(ortho.height),
            "resolution_m": optional_float(ortho.resolution),
            "extent": {name: optional_float(getattr(ortho, name, None))
                       for name in ("left", "right", "bottom", "top")},
        })
    return {"chunk_key": chunk.key, "active_orthomosaic_key": active,
            "orthomosaic_count": len(rows), "orthomosaics": rows}


def compare_elevations(doc, request):
    chunk_a = find_chunk(doc, request["chunk_a_key"])
    chunk_b = find_chunk(doc, request["chunk_b_key"])
    elevation_a = find_elevation(chunk_a, request.get("elevation_a_key"))
    elevation_b = find_elevation(chunk_b, request.get("elevation_b_key"))
    crs_a = coordinate_system_summary(elevation_a.crs or chunk_a.crs)
    crs_b = coordinate_system_summary(elevation_b.crs or chunk_b.crs)
    if crs_a != crs_b:
        raise ValueError("DEM имеют разные CRS. Сначала явно приведите их к одной системе координат.")
    if chunk_a.key != chunk_b.key and (crs_a is None or not crs_a.get("authority")):
        raise ValueError("Для сравнения DEM разных чанков нужна одинаковая авторитетная CRS.")
    left = max(float(elevation_a.left), float(elevation_b.left))
    right = min(float(elevation_a.right), float(elevation_b.right))
    bottom = max(float(elevation_a.bottom), float(elevation_b.bottom))
    top = min(float(elevation_a.top), float(elevation_b.top))
    if not left < right or not bottom < top:
        raise ValueError("У DEM нет общей области в их системе координат.")
    size = int(request["grid_size"])
    differences = []
    invalid = 0
    for row in range(size):
        y = bottom + (row + 0.5) * (top - bottom) / size
        for column in range(size):
            x = left + (column + 0.5) * (right - left) / size
            try:
                a = float(elevation_a.altitude(Metashape.Vector([x, y])))
                b = float(elevation_b.altitude(Metashape.Vector([x, y])))
            except Exception:
                invalid += 1
                continue
            if math.isfinite(a) and math.isfinite(b):
                differences.append(b - a)
            else:
                invalid += 1
    if not differences:
        raise ValueError("В общей области не найдено ни одной пары действительных высот.")
    return {
        "surface_a": elevation_statistics(chunk_a, elevation_a.key),
        "surface_b": elevation_statistics(chunk_b, elevation_b.key),
        "coordinate_system": crs_a,
        "overlap": {"left": left, "right": right, "bottom": bottom, "top": top},
        "sampling": {"grid_size": size, "requested_points": size * size,
                     "valid_pairs": len(differences), "invalid_pairs": invalid},
        "difference_b_minus_a": numeric_summary(differences),
        "units_note": "Разность имеет вертикальные единицы CRS. Положительное значение означает, что B выше A.",
        "volume_note": "Объём не вычислялся: регулярная выборка не заменяет интегрирование DEM с проверкой горизонтальных единиц и ячеек NoData.",
    }


def capture_viewport(app, width=1280, height=720):
    handle, path = tempfile.mkstemp(prefix="metashape-mcp-viewport-", suffix=".png")
    os.close(handle)
    try:
        image = app.model_view.captureView(width=width, height=height,
                                           transparent=False, hide_items=False)
        image.save(path)
        data = Path(path).read_bytes()
        if not data:
            raise RuntimeError("Metashape вернула пустой снимок viewport.")
        return {"mime_type": "image/png", "width": int(image.width), "height": int(image.height),
                "base64": base64.b64encode(data).decode("ascii")}
    finally:
        Path(path).unlink(missing_ok=True)


def chunk_assets(chunk):
    result = {"chunk_key": chunk.key, "frame_index": chunk.frames.index(chunk.frame),
              "has_tie_points": chunk.tie_points is not None}
    for field, default in (("depth_maps_sets", "depth_maps"), ("point_clouds", "point_cloud"),
                           ("models", "model"), ("tiled_models", "tiled_model"),
                           ("elevations", "elevation"), ("orthomosaics", "orthomosaic")):
        active = getattr(chunk, default)
        entries = []
        for asset in getattr(chunk, field):
            entry = {"key": asset.key, "label": asset.label,
                     "default": active is not None and asset.key == active.key}
            if field == "models":
                entry["texture_count"] = len(asset.textures)
            entries.append(entry)
        result[field] = entries
    return result


MASK_OPERATIONS = {
    "replacement": "MaskOperationReplacement", "union": "MaskOperationUnion",
    "intersection": "MaskOperationIntersection", "difference": "MaskOperationDifference",
}


def epsg_crs(code):
    """Metashape coordinate system for an EPSG code, with a readable error for unknown codes."""
    try:
        crs = Metashape.CoordinateSystem(f"EPSG::{int(code)}")
    except Exception as error:
        raise ValueError(f"Metashape не знает систему координат EPSG:{code} ({error}).") from error
    if not crs.name:
        raise ValueError(f"Metashape не знает систему координат EPSG:{code}.")
    return crs


POINT_CLOUD_FORMATS = {"las": "PointCloudFormatLAS", "laz": "PointCloudFormatLAZ",
                       "e57": "PointCloudFormatE57", "ply": "PointCloudFormatPLY"}


def metashape_constant(enum_name, value):
    """Read a Metashape enum value from the module or its enum class."""
    constant = getattr(Metashape, value, None)
    if constant is None:
        constant = getattr(getattr(Metashape, enum_name), value)
    return constant


def mask_target_cameras(chunk, camera_keys):
    """Regular cameras of the chunk, or exactly the requested keys (all must exist)."""
    regular = [camera for camera in chunk.cameras if camera.type == Metashape.Camera.Type.Regular]
    if camera_keys is None:
        return regular
    by_key = {camera.key: camera for camera in regular}
    missing = sorted(set(camera_keys) - set(by_key))
    if missing:
        raise ValueError(f"Обычные камеры с ключами не найдены: {missing[:20]}")
    return [by_key[key] for key in camera_keys]


CHUNK_COPY_ITEMS = {
    "tie_points": "TiePointsData", "depth_maps": "DepthMapsData", "point_clouds": "PointCloudData",
    "models": "ModelData", "tiled_models": "TiledModelData", "elevations": "ElevationData",
    "orthomosaics": "OrthomosaicData", "masks": "MasksData",
}
CHUNK_ALIGNMENT_METHODS = {"points": 0, "markers": 1, "cameras": 2}
MERGE_OPTIONS = ("copy_laser_scans", "copy_masks", "copy_depth_maps", "copy_point_clouds", "copy_models",
                 "copy_tiled_models", "copy_elevations", "copy_orthomosaics", "merge_markers",
                 "merge_tiepoints", "merge_assets")


def matrix_values(matrix):
    if matrix is None:
        return None
    return [float(matrix[row, column]) for row in range(4) for column in range(4)]


def marker_projection_count(marker):
    try:
        return len(list(marker.projections.keys()))
    except Exception:
        return 0


def chunk_profile(chunk):
    """Compact description of a chunk used for compatibility checks and plan fingerprints."""
    regular = [camera for camera in chunk.cameras if camera.type == Metashape.Camera.Type.Regular]
    return {
        "key": chunk.key, "label": chunk.label, "enabled": chunk.enabled,
        "coordinate_system": coordinate_system_summary(chunk.crs),
        "transform": matrix_values(chunk.transform.matrix),
        "camera_count": len(regular),
        "aligned_cameras": sum(camera.transform is not None for camera in regular),
        "aligned_camera_labels": sorted(camera.label for camera in regular if camera.transform is not None),
        "markers": {marker.label: {"projections": marker_projection_count(marker),
                                   "has_reference": marker.reference.location is not None}
                    for marker in chunk.markers},
        "assets": {"tie_points": chunk.tie_points is not None,
                   "depth_maps": len(chunk.depth_maps_sets), "point_clouds": len(chunk.point_clouds),
                   "models": len(chunk.models), "elevations": len(chunk.elevations),
                   "orthomosaics": len(chunk.orthomosaics)},
    }


def chunks_fingerprint(doc, chunks):
    state = {"project": doc.path, "chunks": [chunk_profile(chunk) for chunk in chunks],
             "cameras": [[(camera.key, camera.enabled, camera.transform is not None)
                          for camera in chunk.cameras] for chunk in chunks]}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def find_chunks(doc, keys):
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("Список чанков должен быть непустым и без повторов.")
    return [find_chunk(doc, key) for key in keys]


def pair_compatibility(reference, other):
    """What each Align Chunks method needs between the reference and another chunk."""
    ref, oth = chunk_profile(reference), chunk_profile(other)
    shared_markers = sorted(label for label in set(ref["markers"]) & set(oth["markers"])
                            if ref["markers"][label]["projections"] >= 2
                            and oth["markers"][label]["projections"] >= 2)
    shared_cameras = sorted(set(ref["aligned_camera_labels"]) & set(oth["aligned_camera_labels"]))
    both_aligned = ref["aligned_cameras"] > 0 and oth["aligned_cameras"] > 0
    methods = {
        "points": {"possible": both_aligned,
                   "reason": None if both_aligned else "в обоих чанках нужны выровненные камеры"},
        "markers": {"possible": len(shared_markers) >= 3, "shared_markers": shared_markers,
                    "reason": None if len(shared_markers) >= 3 else
                    "нужно не меньше 3 общих маркеров (по имени) с проекциями минимум на 2 снимках в каждом чанке"},
        "cameras": {"possible": len(shared_cameras) >= 3, "shared_aligned_cameras": len(shared_cameras),
                    "reason": None if len(shared_cameras) >= 3 else
                    "нужно не меньше 3 общих (по имени) выровненных камер"},
    }
    return {"chunk_key": other.key, "label": other.label,
            "same_coordinate_system": ref["coordinate_system"] == oth["coordinate_system"],
            "methods": methods}


def chunk_summary_row(chunk):
    profile = chunk_profile(chunk)
    return {key: profile[key] for key in ("key", "label", "camera_count", "aligned_cameras",
                                           "coordinate_system", "assets")} | {
        "marker_count": len(profile["markers"])}


def chunk_plan(doc, chunks, summary):
    reference = chunks[0]
    return {"project": doc.path, "chunk_label": reference.label, "total": len(chunks),
            "frame_index": reference.frames.index(reference.frame),
            "fingerprint": chunks_fingerprint(doc, chunks), "summary": summary}


def directory_bytes(path):
    """Size of the real files below path (macOS AppleDouble ._ files excluded)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            if not name.startswith("._"):
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return total


def remove_chunk_quarantined(doc, chunk):
    """Remove a chunk from a .psx project and save; its data folder goes to quarantine first.

    probe 0.33: Metashape deletes part of a removed chunk's data (tie points) on remove/save and
    leaves the rest (depth maps, models) as orphans, so the whole folder is moved aside first.
    """
    if not doc.path or Path(doc.path).suffix.lower() != ".psx":
        raise ValueError("Удаление чанка с карантином данных возможно только в проекте .psx.")
    if len(doc.chunks) < 2:
        raise ValueError("Нельзя удалить единственный чанк проекта.")
    files = Path(doc.path).with_suffix(".files")
    data = files / str(chunk.key)
    summary = chunk_summary_row(chunk)
    data_bytes = directory_bytes(data) if data.is_dir() else 0
    moved = []
    if data.is_dir():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        quarantine = (files.parent / ".metashape-mcp-quarantine"
                      / f"{files.stem}.chunk{summary['key']}-{stamp}")
        quarantine.parent.mkdir(exist_ok=True)
        os.rename(data, quarantine)  # same volume: instant, never a silent copy
        moved.append({"original_path": str(data), "quarantine_path": str(quarantine),
                      "bytes": data_bytes})
    try:
        doc.remove([chunk])
        doc.save()
    except Exception:
        # put the data back so nothing is left unrecorded in quarantine
        if moved and not data.exists():
            os.rename(moved[0]["quarantine_path"], data)
        raise
    quarantined_bytes = directory_bytes(moved[0]["quarantine_path"]) if moved else 0
    return {"removed_chunk": summary, "data_bytes": data_bytes,
            "quarantined_bytes": quarantined_bytes,
            "quarantine_complete": quarantined_bytes == data_bytes,
            "data_folder_recreated": data.exists(), "moved_data": moved}


def optional_float(value):
    return None if value is None else float(value)


def model_statistics(model):
    """Geometry figures of one model; volume only for a closed surface (no open edges)."""
    stats = model.statistics()
    fields = ("components", "degenerate_faces", "duplicate_faces", "flipped_normals", "free_vertices",
              "invalid_vertices", "multiple_edges", "open_edges", "out_of_range_indices",
              "similar_vertices", "zero_faces")
    topology = {name: int(getattr(stats, name)) for name in fields if hasattr(stats, name)}
    closed = topology.get("open_edges") == 0
    return {"key": model.key, "label": model.label, "faces": len(model.faces),
            "vertices": len(model.vertices), "textures": len(model.textures),
            "area": optional_float(model.area()), "closed_surface": closed,
            "volume": optional_float(model.volume()) if closed else None, "topology": topology,
            "topology_problems": sum(value for name, value in topology.items()
                                     if name not in ("components", "open_edges"))}


def model_spatial_bounds(model):
    transform = model.transform
    return bounds_from_points(
        transform.mulp(vertex.coord) if transform is not None else vertex.coord
        for vertex in model.vertices)


MODEL_EXPORTS = {  # name: (ModelFormat constant, file name, binary)
    "obj": ("ModelFormatOBJ", "model.obj", True), "ply": ("ModelFormatPLY", "model.ply", True),
    "stl": ("ModelFormatSTL", "model.stl", True), "fbx": ("ModelFormatFBX", "model.fbx", True),
    # ModelFormatGLTF always writes binary glTF (GLB) in 2.2.0, even with binary=False (probe 0.27),
    # so only "glb" is offered.
    "glb": ("ModelFormatGLTF", "model.glb", True),
}
CLOUD_EXPORTS = {"las": ("PointCloudFormatLAS", "cloud.las"), "laz": ("PointCloudFormatLAZ", "cloud.laz"),
                 "e57": ("PointCloudFormatE57", "cloud.e57"), "ply": ("PointCloudFormatPLY", "cloud.ply")}
# COLMAP: path is a file; Metashape writes cameras.txt, images.txt, points3D.txt (and images/ when
# save_images) into its parent folder. Creating a folder at the path made the 2.2.0 export hang.
CAMERA_EXPORTS = {"xml": ("CamerasFormatXML", "cameras.xml"), "colmap": ("CamerasFormatColmap", "colmap_layout.txt"),
                  "bundler": ("CamerasFormatBundler", "bundler.out"), "opk": ("CamerasFormatOPK", "opk.txt"),
                  "nvm": ("CamerasFormatNVM", "cameras.nvm"), "fbx": ("CamerasFormatFBX", "cameras.fbx")}


def boundary_shape_count(chunk):
    shapes = chunk.shapes
    if shapes is None:
        return 0
    count = 0
    for shape in shapes:
        kind = str(getattr(shape, "boundary_type", "NoBoundary")).split(".")[-1]
        if kind != "NoBoundary":
            count += 1
    return count


def output_files(folder):
    files = sorted(path for path in Path(folder).rglob("*")
                   if path.is_file() and not path.name.startswith("."))
    empty = [str(path) for path in files if not path.stat().st_size]
    if not files or empty:
        raise RuntimeError("Metashape не создала файлы экспорта" + (f": пустые {empty[:5]}" if empty else "."))
    return [{"path": str(path), "bytes": path.stat().st_size} for path in files]


def ensure_new_file(path):
    """Metashape answers a write error with a modal "retry?" dialog that blocks the MCP call,
    so every output path is checked before the call."""
    path = Path(path)
    if path.exists():
        raise ValueError(f"Путь вывода уже существует: {path}")
    if not path.parent.is_dir() or not os.access(path.parent, os.W_OK):
        raise ValueError(f"Нет доступной для записи папки: {path.parent}")
    return str(path)


def app_document():
    return Metashape.app.document


def render_views(doc, camera_chunk, options, folder):
    """Render the models of several chunks from the same real camera viewpoints, plus the photo."""
    cameras = {camera.key: camera for camera in camera_chunk.cameras}
    targets = [find_chunk(doc, key) for key in options["chunk_keys"]]
    without = [chunk.label for chunk in targets if chunk.model is None]
    if without:
        raise ValueError(f"В чанках нет активной модели: {without}")
    if camera_chunk.transform.matrix is None or any(c.transform.matrix is None for c in targets):
        raise ValueError("У чанков нет положения в общей системе координат (transform).")
    scale = options["scale"]
    rows = []
    for position, camera_key in enumerate(options["camera_keys"]):
        TRACKER(100.0 * position / len(options["camera_keys"]))
        camera = cameras.get(camera_key)
        if camera is None or camera.transform is None:
            raise ValueError(f"Камера {camera_key} не найдена или не выровнена в чанке {camera_chunk.label}.")
        source = camera.sensor.calibration
        calibration = Metashape.Calibration()
        calibration.width, calibration.height = int(source.width * scale), int(source.height * scale)
        calibration.f, calibration.cx, calibration.cy = source.f * scale, source.cx * scale, source.cy * scale
        world = camera_chunk.transform.matrix * camera.transform
        label = camera.label
        photo_path = ensure_new_file(folder / f"{label}__photo.jpg")
        camera.photo.image().resize(calibration.width, calibration.height).save(photo_path)
        files = {"photo": photo_path}
        for chunk in targets:
            local = chunk.transform.matrix.inv() * world
            image = chunk.model.renderImage(local, calibration, cull_faces=True, add_alpha=False)
            path = ensure_new_file(folder / f"{label}__chunk{chunk.key}.jpg")
            image.save(path)
            files[f"chunk{chunk.key}"] = path
        rows.append({"camera_key": camera_key, "camera": label, "files": files})
    return {"views": rows, "note": "Рендер без учёта дисторсии объектива; сравнивайте модели между собой "
                                   "и с уменьшенным снимком."}


def export_asset(chunk, request, folder):
    """Run one typed export into a fresh folder; return details for the result."""
    kind, options = request["kind"], request["options"]
    crs = {"crs": epsg_crs(options["epsg"])} if options.get("epsg") else {}
    details = {}
    if kind == "model":
        model = chunk.model if options.get("model_key") is None else next(
            (item for item in chunk.models if item.key == options["model_key"]), None)
        if model is None or not len(model.faces):
            raise ValueError("Нет модели с гранями для экспорта (активной или с указанным ключом).")
        constant, name, binary = MODEL_EXPORTS[options["format"]]
        textured = bool(model.textures) and options["save_texture"]
        path = Path(ensure_new_file(folder / name))
        # Same-chunk round-trip must stay in model-local coordinates. Metashape 2.2 export with
        # a geographic chunk CRS followed by importModel(crs=same_crs) is not symmetric in a
        # live probe. An EPSG is therefore used only when the caller explicitly asks for one.
        effective_crs = epsg_crs(options["epsg"]) if options.get("epsg") else None
        model_crs = {"shift": Metashape.Vector([0.0, 0.0, 0.0])}
        if effective_crs is not None:
            model_crs["crs"] = effective_crs
        chunk.exportModel(progress=TRACKER,
            path=str(path), format=metashape_constant("ModelFormat", constant), binary=binary,
            precision=15,
            model=model.key, save_texture=textured, save_uv=options["save_uv"],
            save_normals=options["save_normals"], save_colors=options["save_colors"],
            embed_texture=options["format"] == "glb" and textured,
            gltf_y_up=True, clip_to_boundary=options["clip_to_boundary"], **model_crs)
        coordinate_space = "epsg" if options.get("epsg") else "chunk_internal"
        document = app_document()
        manifest = {
            "schema": SCHEMA,
            "source": {
                "project_path": str(Path(document.path).resolve()) if document.path else None,
                "chunk_key": chunk.key,
                "chunk_label": chunk.label,
                "chunk_transform": matrix_values(chunk.transform.matrix),
                "chunk_coordinate_system": coordinate_system_summary(chunk.crs),
                "model_key": model.key,
                "model_label": model.label,
                "model_transform": matrix_values(model.transform),
                "vertices": len(model.vertices),
                "faces": len(model.faces),
                "local_bounds": model_spatial_bounds(model),
            },
            "export": {
                "format": options["format"],
                "coordinate_space": coordinate_space,
                "coordinate_system": coordinate_system_summary(effective_crs),
                "epsg": options.get("epsg"),
                "shift": [0.0, 0.0, 0.0],
                "precision": 15,
                "gltf_y_up": True if options["format"] == "glb" else None,
                "clip_to_boundary": bool(options["clip_to_boundary"]),
            },
            "model_file": {"name": path.name, "fingerprint": file_fingerprint(path)},
            "roundtrip_contract": {
                "import_crs": "same as export",
                "import_shift": "same explicit vector as export",
                "target": "same saved project and exact chunk key",
                "validation": "chunk transform, CRS and local model bounds",
            },
        }
        manifest_path = folder / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        details = {"model_key": model.key, "faces": len(model.faces),
                   "texture_exported": textured,
                   "coordinate_space": coordinate_space,
                   "coordinate_system": coordinate_system_summary(effective_crs),
                   "coordinate_manifest": str(manifest_path)}
    elif kind == "point_cloud":
        cloud = chunk.point_cloud if options.get("point_cloud_key") is None else next(
            (item for item in chunk.point_clouds if item.key == options["point_cloud_key"]), None)
        if cloud is None:
            raise ValueError("Нет облака точек для экспорта (активного или с указанным ключом).")
        constant, name = CLOUD_EXPORTS[options["format"]]
        extra = {"classes": options["classes"]} if options.get("classes") else {}
        chunk.exportPointCloud(progress=TRACKER,
            path=ensure_new_file(folder / name), format=metashape_constant("PointCloudFormat", constant),
            source_data=Metashape.PointCloudData, point_clouds=[cloud.key],
            save_point_color=options["save_point_color"],
            save_point_classification=options["save_point_classification"],
            clip_to_boundary=options["clip_to_boundary"], **crs, **extra)
        details = {"point_cloud_key": cloud.key, "point_count": int(cloud.point_count)}
    elif kind == "tiled_model":
        tiled_model = chunk.tiled_model if options.get("tiled_model_key") is None else next(
            (item for item in chunk.tiled_models
             if item.key == options["tiled_model_key"]), None)
        if tiled_model is None:
            raise ValueError("Нет тайловой модели (активной или с указанным ключом).")
        spec = tiled_model_export_spec(
            options["format"], options["tileset_version"], options["model_compression"],
            options["use_tileset_transform"], options["screen_space_error"],
            options["folder_depth"])
        effective_crs = epsg_crs(options["epsg"]) if options.get("epsg") else (
            tiled_model.crs or chunk.crs)
        if (options["format"] == "slpk"
                and (effective_crs is None or not effective_crs.authority)):
            raise ValueError("SLPK требует привязанной системы координат с EPSG. "
                             "Укажите epsg или сначала привяжите чанк.")
        chunk.exportTiledModel(
            progress=TRACKER, path=ensure_new_file(folder / spec["filename"]),
            format=metashape_constant("TiledModelFormat", spec["constant"]),
            tiled_model=tiled_model.key, clip_to_boundary=options["clip_to_boundary"],
            **spec["arguments"], **crs)
        details = {
            "tiled_model_key": tiled_model.key,
            "tiled_model_label": tiled_model.label,
            "tiled_model_resolution": optional_float(tiled_model.resolution),
            "coordinate_system": coordinate_system_summary(effective_crs),
            "cesium_options_applied": spec["cesium_options_applied"],
        }
    elif kind == "cameras":
        constant, name = CAMERA_EXPORTS[options["format"]]
        chunk.exportCameras(progress=TRACKER, path=ensure_new_file(folder / name), format=metashape_constant("CamerasFormat", constant),
                            save_points=options["save_points"], save_markers=options["save_markers"],
                            save_images=options["save_images"], **crs)
        details = {"aligned_cameras": sum(camera.transform is not None for camera in chunk.cameras),
                   "images_exported": options["save_images"]}
        if options["format"] == "colmap" and Metashape.app.version.startswith("2.2"):
            details["warning"] = ("Metashape 2.2 пишет p1/p2 в sparse/0/cameras.txt в своём порядке "
                                  "(проверено: p2 совпадает с калибровкой). По данным сообщества, "
                                  "у OpenCV/COLMAP порядок обратный и исправлен только в 2.3.0 build "
                                  "21778; при необходимости поменяйте p1 и p2 местами или используйте "
                                  "неискажённые снимки. Экспорт также пересчитывает модель дисторсии "
                                  "(появляются k2/k3).")
    elif kind == "sensor_calibration":
        sensor = next((item for item in chunk.sensors
                       if item.key == options["sensor_key"]), None)
        if sensor is None:
            raise ValueError(f"Сенсор {options['sensor_key']} не найден.")
        calibration = (sensor.calibration if options["source"] == "adjusted"
                       else sensor.user_calib)
        if calibration is None:
            raise ValueError(f"У сенсора нет калибровки source={options['source']}.")
        name = f"sensor-{sensor.key}-{options['source']}.xml"
        path = ensure_new_file(folder / name)
        calibration.save(path)
        details = {"sensor_key": sensor.key, "sensor_label": sensor.label,
                   "source": options["source"], "calibration_file": path}
    elif kind == "shapes":
        if not shape_count(chunk):
            raise ValueError("В чанке нет фигур для экспорта.")
        require_geographic(chunk, options["format"])
        chunk.exportShapes(progress=TRACKER, path=ensure_new_file(folder / SHAPE_FORMATS[options["format"]][1]),
                           format=shapes_format(options["format"]), save_points=options["save_points"],
                           save_polylines=options["save_polylines"], save_polygons=options["save_polygons"],
                           **crs)
        details = {"shape_count": shape_count(chunk)}
    elif kind == "renders":
        details = render_views(app_document(), chunk, options, folder)
    elif kind == "report":
        chunk.exportReport(progress=TRACKER, path=ensure_new_file(folder / "report.pdf"), title=options["title"],
                           description=options["description"],
                           include_system_info=options["include_system_info"])
    else:
        raise ValueError("Неизвестный вид экспорта.")
    return details


SHAPE_FORMATS = {"shp": ("ShapesFormatSHP", "shapes.shp"), "dxf": ("ShapesFormatDXF", "shapes.dxf"),
                 "geojson": ("ShapesFormatGeoJSON", "shapes.geojson"), "kml": ("ShapesFormatKML", "shapes.kml")}
BOUNDARY_NAMES = {"none": "NoBoundary", "outer": "OuterBoundary", "inner": "InnerBoundary"}


def shapes_format(name):
    constant = SHAPE_FORMATS[name][0]
    return getattr(Metashape, constant, None) or getattr(Metashape.ShapesFormat, constant)


def shape_count(chunk):
    return len(chunk.shapes) if chunk.shapes is not None else 0


def require_geographic(chunk, fmt):
    """GeoJSON/KML need WGS84; a local CRS makes Metashape open a modal error dialog."""
    if fmt in ("geojson", "kml") and (chunk.crs is None or not chunk.crs.authority):
        raise ValueError("GeoJSON и KML требуют географической системы координат; у чанка "
                         f"«{chunk.crs.name if chunk.crs is not None else 'нет'}». Выберите shp или dxf.")


def region_summary(chunk):
    region = chunk.region
    return {"chunk_key": chunk.key, "center": vector_values(region.center),
            "size": vector_values(region.size),
            "rotation": [float(region.rot[row, column]) for row in range(3) for column in range(3)],
            "units": "внутренние координаты чанка"}


def point_cloud_statistics(chunk, point_cloud_key=None):
    clouds = list(chunk.point_clouds)
    if point_cloud_key is not None:
        clouds = [cloud for cloud in clouds if cloud.key == point_cloud_key]
        if not clouds:
            raise ValueError("Облако точек с указанным ключом не найдено.")
    active_key = chunk.point_cloud.key if chunk.point_cloud is not None else None
    rows = []
    for cloud in clouds:
        extent = cloud.extent()
        rows.append({
            "key": cloud.key,
            "label": cloud.label,
            "enabled": cloud.enabled,
            "active": cloud.key == active_key,
            "point_count": int(cloud.point_count),
            "class_counts": [
                {"class_id": int(class_id), "count": int(count)}
                for class_id, count in sorted(cloud.point_count_by_class.items())
            ],
            "extent": {
                "minimum": vector_values(extent.min),
                "maximum": vector_values(extent.max),
                "coordinate_space": "point_cloud",
            },
            "coordinate_system": coordinate_system_summary(cloud.crs),
            "data_type": str(cloud.data_type).split(".")[-1],
        })
    return {
        "chunk_key": chunk.key,
        "active_point_cloud_key": active_key,
        "point_cloud_count": len(chunk.point_clouds),
        "point_clouds": rows,
    }


def coordinate_vertex_count(value):
    if value is None:
        return 0
    if isinstance(value, Metashape.Vector):
        return 1
    if isinstance(value, (list, tuple)):
        return sum(coordinate_vertex_count(item) for item in value)
    return 1


def geometry_vertex_count(geometry):
    if geometry is None:
        return 0
    coordinates = getattr(geometry, "coordinates", None) or []
    children = getattr(geometry, "geometries", None) or []
    return coordinate_vertex_count(coordinates) + sum(
        geometry_vertex_count(child) for child in children)


def shape_measurement(shape, volume_level, elevation_available):
    geometry = shape.geometry
    geometry_type = str(geometry.type).split(".")[-1] if geometry is not None else None
    geometry_is_3d = bool(geometry is not None and geometry.is_3d)
    polygon = geometry_type is not None and "Polygon" in geometry_type
    linear = polygon or (geometry_type is not None and "LineString" in geometry_type)
    errors = {}

    def measure(name, function):
        try:
            value = function()
            value = None if value is None else float(value)
            if value is not None and not math.isfinite(value):
                raise ValueError("Metashape вернула неконечное значение")
            return value
        except Exception as error:
            errors[name] = str(error) or type(error).__name__
            return None

    result = {
        "key": shape.key, "label": shape.label,
        "group": ({"key": shape.group.key, "label": shape.group.label}
                  if shape.group is not None else None),
        "geometry_type": geometry_type, "vertex_count": geometry_vertex_count(geometry),
        "geometry_is_3d": geometry_is_3d,
        "is_attached": bool(shape.is_attached),
        "boundary_type": str(shape.boundary_type).split(".")[-1],
        "perimeter_on_dem": (measure("perimeter_on_dem", shape.perimeter2D)
                             if linear and elevation_available else None),
        "perimeter_3d": (measure("perimeter_3d", shape.perimeter3D)
                         if linear and geometry_is_3d else None),
        "area_on_dem": (measure("area_on_dem", shape.area)
                        if polygon and elevation_available else None),
        "area_best_fit_plane": (measure("area_best_fit_plane", shape.areaFitted)
                                if polygon and geometry_is_3d else None),
        "volume_level": (volume_level if polygon and volume_level != "none" else None),
        "volume_on_dem": None,
    }
    if polygon and elevation_available and volume_level != "none":
        try:
            volume = shape.volume(level=volume_level)
            result["volume_on_dem"] = {
                str(key): float(value) for key, value in dict(volume).items()}
        except Exception as error:
            errors["volume_on_dem"] = str(error) or type(error).__name__
    result["measurement_errors"] = errors
    return result


def coordinate_system_summary(crs):
    if crs is None:
        return None
    return {"name": crs.name or None, "authority": crs.authority or None,
            "proj4": crs.proj4 or None}


def camera_reference_summary(camera):
    reference = camera.reference
    return {
        "key": camera.key, "label": camera.label, "enabled": camera.enabled,
        "reference_enabled": reference.enabled,
        "location": vector_values(reference.location),
        "location_accuracy": vector_values(reference.location_accuracy),
        "rotation": vector_values(reference.rotation),
        "rotation_accuracy": vector_values(reference.rotation_accuracy),
    }


def reference_summary(chunk):
    cameras = [camera for camera in chunk.cameras
               if camera.type == Metashape.Camera.Type.Regular]
    return {
        "chunk_key": chunk.key,
        "coordinate_system": coordinate_system_summary(chunk.crs),
        "camera_coordinate_system": coordinate_system_summary(chunk.camera_crs),
        "marker_coordinate_system": coordinate_system_summary(chunk.marker_crs),
        "camera_count": len(cameras),
        "cameras_with_location": sum(camera.reference.location is not None for camera in cameras),
        "enabled_camera_references": sum(camera.reference.enabled and camera.reference.location is not None
                                         for camera in cameras),
        "cameras_with_rotation": sum(camera.reference.rotation is not None for camera in cameras),
        "markers_with_location": sum(marker.reference.location is not None for marker in chunk.markers),
        "enabled_marker_references": sum(marker.reference.enabled and marker.reference.location is not None
                                         for marker in chunk.markers),
        "transform_available": chunk.transform.matrix is not None,
    }


def reference_error_statistics(chunk):
    """Summarize enabled reference-position residuals in local metric axes when CRS is known."""
    matrix = chunk.transform.matrix

    def error_vector(point, reference_location, reference_crs):
        if matrix is None or point is None or reference_location is None:
            return None
        estimated = matrix.mulp(point)
        crs = reference_crs or chunk.crs
        if crs is None:
            return estimated - reference_location
        target = crs.unproject(reference_location)
        return crs.localframe(target).mulv(estimated - target)

    def summarize(entries, kind, crs, role="control"):
        """role=control: enabled references; role=check: located but disabled (check points)."""
        residuals = []
        available = 0
        enabled = 0
        for item in entries:
            reference = item.reference
            if reference.location is not None:
                available += 1
            if reference.location is None:
                continue
            if reference.enabled != (role == "control"):
                continue
            enabled += 1
            point = item.center if kind == "camera" else item.position
            error = error_vector(point, reference.location, crs)
            if error is None:
                continue
            components = vector_values(error)
            magnitude = float(error.norm())
            accuracy = (reference.location_accuracy if kind == "camera"
                        else reference.accuracy)
            normalized = None
            if accuracy is not None and all(value > 0 for value in accuracy):
                normalized = math.sqrt(sum(
                    (components[index] / float(accuracy[index])) ** 2
                    for index in range(3)))
            entry = {
                "key": item.key, "label": item.label,
                "error_xyz": components, "error_3d": magnitude,
                "normalized_error": normalized,
            }
            if kind == "marker":
                entry["projections"] = marker_projection_count(item)
                if entry["projections"] < 3:
                    entry["warning"] = "меньше 3 проекций: по руководству точка не годится как опорная или контрольная"
            residuals.append(entry)
        squared = [item["error_3d"] ** 2 for item in residuals]
        normalized = [item["normalized_error"] for item in residuals
                      if item["normalized_error"] is not None]
        worst = sorted(residuals, key=lambda item: item["error_3d"], reverse=True)[:10]
        return {
            "with_location": available, "enabled_references": enabled,
            "residual_count": len(residuals),
            "rms_3d": math.sqrt(sum(squared) / len(squared)) if squared else None,
            "max_3d": math.sqrt(max(squared)) if squared else None,
            "normalized_rms": math.sqrt(sum(value ** 2 for value in normalized) / len(normalized))
            if normalized else None,
            "worst": worst,
        }

    cameras = [camera for camera in chunk.cameras
               if camera.type == Metashape.Camera.Type.Regular]
    return {
        "chunk_key": chunk.key,
        "units": "meters" if any(
            value is not None for value in (chunk.crs, chunk.camera_crs, chunk.marker_crs)
        ) else "chunk_coordinate_units",
        "transform_available": matrix is not None,
        "cameras": summarize(cameras, "camera", chunk.camera_crs),
        "markers": summarize(chunk.markers, "marker", chunk.marker_crs),
        "check_points": summarize(chunk.markers, "marker", chunk.marker_crs, role="check"),
        "warnings": ([] if sum(m.reference.enabled and m.reference.location is not None
                               for m in chunk.markers)
                     + sum(c.reference.enabled and c.reference.location is not None for c in cameras) >= 3
                     else ["Опорных точек меньше трёх: чанк не привязан к опорным координатам, "
                           "ошибки контрольных точек не оценивают точность."]),
        "check_point_note": ("Контрольные точки (опорные данные выключены) не участвуют в привязке и "
                             "оптимизации; их положение триангулируется по проекциям, поэтому ошибка — "
                             "независимая оценка точности."),
    }


def read_host(request):
    # This callback runs on Metashape's GUI thread; no API objects leave this process.
    app = Metashape.app
    method = request["method"]
    if method == "get_service_status":
        result = {"metashape_version": app.version, "metashape_pid": os.getpid(),
                  "operation": TRACKER.snapshot()}
        if app.version == "2.2.0":
            result["api_reference"] = "https://www.agisoft.com/pdf/metashape_python_api_2_2_0.pdf"
        return result
    if method == "list_geoids":
        rows = []
        for value in Metashape.CoordinateSystem.listGeoids():
            if isinstance(value, str):
                rows.append({"path": value})
            else:
                rows.append({
                    "name": getattr(value, "name", None) or None,
                    "authority": getattr(value, "authority", None) or None,
                    "proj4": getattr(value, "proj4", None) or None,
                    "path": getattr(value, "path", None) or None,
                    "value": str(value),
                })
        return {"count": len(rows), "geoids": rows,
                "note": "Команда только читает уже зарегистрированные геоиды."}
    if method == "progress_cancel":
        return {"job_id": request["job_id"], "signalled": TRACKER.request_cancel(request["job_id"])}
    if method == "get_project_summary":
        doc = app.document
        if doc is None:
            return {"state": "no_document", "path": None, "name": None, "read_only": False,
                    "modified": False, "chunk_count": 0, "active_chunk": None}
        return project_summary(doc)
    if method == "capture_viewport":
        return capture_viewport(app, request.get("width", 1280), request.get("height", 720))
    if method == "create_project":
        doc = app.document
        ensure_replaceable(doc)
        path = project_path(request["path"], existing=False)
        new_document = Metashape.Document()
        new_document.save(str(path))
        del new_document
        doc.open(str(path))
        return project_summary(doc)
    if method == "open_project":
        doc = app.document
        ensure_replaceable(doc)
        path = project_path(request["path"], existing=True)
        current_path = Path(doc.path).resolve() if doc.path else None
        if current_path != path:
            doc.open(str(path))
        return project_summary(doc)
    if method == "save_project":
        doc = app.document
        ensure_writable(doc)
        if not doc.path:
            raise ValueError("У проекта нет пути. Используйте save_project_as.")
        doc.save()
        return project_summary(doc)
    if method == "save_project_as":
        doc = app.document
        ensure_writable(doc)
        path = project_path(request["path"], existing=False)
        doc.save(str(path))
        return project_summary(doc)
    if method == "add_chunk":
        doc = app.document
        ensure_writable(doc)
        label = request["label"].strip()
        if not label:
            raise ValueError("Название чанка не должно быть пустым.")
        chunk = doc.addChunk()
        chunk.label = label
        doc.chunk = chunk
        return {"chunk": {"key": chunk.key, "label": chunk.label, "camera_count": 0},
                "project": project_summary(doc)}
    if method == "add_photos":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        paths = []
        for value in request["paths"]:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("Все пути к фотографиям должны быть абсолютными.")
            path = path.resolve()
            if not path.is_file():
                raise ValueError(f"Файл фотографии не найден: {path}")
            paths.append(path)
        if len(set(paths)) != len(paths):
            raise ValueError("Список содержит повторяющиеся пути к фотографиям.")
        project_directory = Path(doc.path).parent if doc.path else Path.cwd()
        existing = set()
        for camera in chunk.cameras:
            if camera.photo is None or not camera.photo.path:
                continue
            path = Path(camera.photo.path)
            existing.add((project_directory / path).resolve() if not path.is_absolute() else path.resolve())
        repeated = [str(path) for path in paths if path in existing]
        if repeated:
            raise ValueError(f"Фотографии уже добавлены в чанк: {', '.join(repeated[:3])}")
        before = {camera.key for camera in chunk.cameras}
        chunk.addPhotos(filenames=[str(path) for path in paths])
        added = [camera_summary(camera) for camera in chunk.cameras if camera.key not in before]
        return {"chunk_key": chunk.key, "requested_count": len(paths), "added_count": len(added),
                "camera_count": len(chunk.cameras), "cameras": added}
    if method == "list_chunks":
        doc = app.document
        chunks = doc.chunks if doc is not None else []
        result = page(chunks, request, "chunks", lambda c: {"key": c.key, "label": c.label,
                      "enabled": c.enabled, "camera_count": len(c.cameras)})
        result["active_chunk_key"] = doc.chunk.key if doc is not None and doc.chunk is not None else None
        return result
    if method == "list_cameras":
        chunk = find_chunk(app.document, request["chunk_key"])
        return {"chunk_key": chunk.key, "frame_index": chunk.frames.index(chunk.frame),
                **page(chunk.cameras, request, "cameras", camera_summary)}
    if method == "list_chunk_assets":
        return chunk_assets(find_chunk(app.document, request["chunk_key"]))
    if method == "get_point_cloud_statistics":
        return point_cloud_statistics(
            find_chunk(app.document, request["chunk_key"]), request.get("point_cloud_key"))
    if method == "get_elevation_statistics":
        return elevation_statistics(
            find_chunk(app.document, request["chunk_key"]), request.get("elevation_key"))
    if method == "get_orthomosaic_statistics":
        return orthomosaic_statistics(find_chunk(app.document, request["chunk_key"]))
    if method == "compare_elevations":
        return compare_elevations(app.document, request)
    if method == "get_shape_measurements":
        chunk = find_chunk(app.document, request["chunk_key"])
        shapes = list(chunk.shapes.shapes) if chunk.shapes is not None else []
        requested = request.get("shape_keys")
        if requested is not None:
            by_key = {shape.key: shape for shape in shapes}
            missing = sorted(set(requested) - set(by_key))
            if missing:
                raise ValueError(f"Фигуры с ключами не найдены: {missing}.")
            shapes = [by_key[key] for key in requested]
        scale = chunk.transform.scale if chunk.transform is not None else None
        shape_crs = chunk.shapes.crs if chunk.shapes is not None else None
        basis = measurement_basis(
            scale, coordinate_system_summary(shape_crs or chunk.crs), chunk.elevation is not None)
        result = page(
            shapes, request, "shapes",
            lambda shape: shape_measurement(
                shape, request["volume_level"], chunk.elevation is not None))
        return {"chunk_key": chunk.key, "measurement_basis": basis,
                "active_elevation": ({"key": chunk.elevation.key,
                                      "label": chunk.elevation.label}
                                     if chunk.elevation is not None else None), **result}
    if method == "get_mask_summary":
        return mask_summary(
            find_chunk(app.document, request["chunk_key"]), request["offset"], request["limit"],
            bool(request.get("include_coverage")),
            lambda camera: camera.type == Metashape.Camera.Type.Regular)
    if method == "mask_import_targets":
        doc = app.document
        chunk = find_chunk(doc, request["chunk_key"])
        cameras = mask_target_cameras(chunk, request.get("camera_keys"))
        project_directory = Path(doc.path).parent if doc.path else None
        rows = []
        for camera in cameras:
            photo = camera.photo.path if camera.photo is not None else ""
            if photo and not Path(photo).is_absolute() and project_directory is not None:
                photo = str((project_directory / photo).resolve())
            rows.append({"key": camera.key, "label": camera.label, "photo_path": photo or None,
                         "has_mask": camera.mask is not None, "aligned": camera.transform is not None})
        return {"chunk_key": chunk.key, "cameras": rows, "has_model": chunk.model is not None}
    if method == "mask_import":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        cameras = mask_target_cameras(chunk, request["camera_keys"])
        before = sum(camera.mask is not None for camera in cameras)
        chunk.generateMasks(progress=TRACKER,
            path=request["template"], masking_mode=metashape_constant("MaskingMode", "MaskingModeFile"),
            mask_operation=metashape_constant("MaskOperation", MASK_OPERATIONS[request["operation"]]),
            cameras=[camera.key for camera in cameras], replace_asset=True)
        without = [camera.label for camera in cameras if camera.mask is None]
        return {"chunk_key": chunk.key, "operation": request["operation"],
                "selected_cameras": len(cameras), "cameras_with_mask_before": before,
                "cameras_with_mask_after": len(cameras) - len(without),
                "cameras_still_without_mask": without[:100], "save_policy": "manual"}
    if method == "mask_generate":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        cameras = mask_target_cameras(chunk, request["camera_keys"])
        before = sum(camera.mask is not None for camera in cameras)
        mode = {"alpha": "MaskingModeAlpha", "background": "MaskingModeBackground",
                "model": "MaskingModeModel"}[request["mode"]]
        options = {}
        if request["mode"] == "background":
            options["path"] = request["template"]
        chunk.generateMasks(progress=TRACKER,
            masking_mode=metashape_constant("MaskingMode", mode),
            mask_operation=metashape_constant("MaskOperation", MASK_OPERATIONS[request["operation"]]),
            tolerance=int(request["tolerance"]), cameras=[camera.key for camera in cameras],
            replace_asset=True, **options)
        without = [camera.label for camera in cameras if camera.mask is None]
        return {"chunk_key": chunk.key, "mode": request["mode"], "operation": request["operation"],
                "tolerance": request["tolerance"], "selected_cameras": len(cameras),
                "cameras_with_mask_before": before,
                "cameras_with_mask_after": len(cameras) - len(without),
                "cameras_still_without_mask": without[:100], "save_policy": "manual"}
    if method == "point_cloud_import_target":
        chunk = find_chunk(app.document, request["chunk_key"])
        epsg_name = None
        if request.get("epsg") is not None:
            epsg_name = epsg_crs(request["epsg"]).name
        active = chunk.point_cloud.key if chunk.point_cloud is not None else None
        return {"chunk_key": chunk.key, "active_point_cloud_key": active,
                "coordinate_system": coordinate_system_summary(chunk.crs), "epsg_name": epsg_name,
                "point_clouds": [{"key": cloud.key, "label": cloud.label,
                                  "point_count": int(cloud.point_count)} for cloud in chunk.point_clouds]}
    if method == "point_cloud_import":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        before = {cloud.key for cloud in chunk.point_clouds}
        if before != set(request["expected_existing"]):
            raise ValueError("Набор облаков точек в чанке изменился после проверки. Создайте новый preview.")
        active_before = chunk.point_cloud.key if chunk.point_cloud is not None else None
        options = {}
        if request.get("epsg") is not None:
            options["crs"] = epsg_crs(request["epsg"])
        chunk.importPointCloud(progress=TRACKER,
            path=request["path"],
            format=metashape_constant("PointCloudFormat", POINT_CLOUD_FORMATS[request["format"]]),
            replace_asset=False, load_point_color=bool(request["load_colors"]),
            calculate_normals=bool(request["calculate_normals"]), **options)
        created = [cloud for cloud in chunk.point_clouds if cloud.key not in before]
        if len(created) != 1:
            raise RuntimeError(f"Ожидалось одно новое облако точек, появилось {len(created)}.")
        cloud = created[0]
        if int(cloud.point_count) <= 0:
            raise RuntimeError("Импортированное облако точек пустое.")
        active = chunk.point_cloud.key if chunk.point_cloud is not None else None
        return {"chunk_key": chunk.key, "point_cloud_key": cloud.key, "label": cloud.label,
                "imported_point_count": int(cloud.point_count),
                "class_counts": [{"class_id": int(k), "count": int(v)}
                                 for k, v in sorted(cloud.point_count_by_class.items())],
                "coordinate_system": coordinate_system_summary(cloud.crs),
                "existing_point_clouds_unchanged": sorted(before),
                "active_point_cloud_key_before": active_before, "active_point_cloud_key": active,
                "save_policy": "manual"}
    if method == "set_active_chunk":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        previous = doc.chunk.key if doc.chunk is not None else None
        doc.chunk = chunk
        return {"active_chunk_key": doc.chunk.key, "previous_active_chunk_key": previous,
                "save_policy": "manual"}
    if method == "autopilot_tasks":
        from autopilot import TASKS
        return {"tasks": CameraAlignment.describe_tasks(TASKS, request.get("task")),
                "notes": ["Параметры-перечисления задаются именем значения строкой, "
                          "например \"source_data\": \"DepthMapsData\"."]}
    if method == "chunk_set_enabled":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        before = bool(chunk.enabled)
        chunk.enabled = bool(request["enabled"])
        return {"chunk_key": chunk.key, "label": chunk.label, "enabled_before": before,
                "enabled": bool(chunk.enabled), "save_policy": "manual"}
    if method == "chunk_remove":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        if chunk.label != request["confirm_label"]:
            raise ValueError(f"confirm_label не совпадает с именем чанка «{chunk.label}».")
        result = remove_chunk_quarantined(doc, chunk)
        return result | {"active_chunk_key": doc.chunk.key if doc.chunk is not None else None,
                         "chunk_count": len(doc.chunks), "save_policy": "saved"}
    if method == "chunk_promote":
        doc = app.document
        ensure_writable(doc)
        source = find_chunk(doc, request["source_key"])
        copy = find_chunk(doc, request["copy_key"])
        if source.key == copy.key:
            raise ValueError("Исходный чанк и копия совпадают.")
        if copy.label != request["copy_label"]:
            raise ValueError(f"Копия переименована или это другой чанк («{copy.label}»); "
                             "решение по заданию отменено.")
        if source.label != request["source_label"]:
            raise ValueError(f"Исходный чанк переименован («{source.label}»); решение отменено.")
        action = request["source_action"]
        if action == "remove" and request.get("confirm_source_label") != source.label:
            raise ValueError("Для удаления исходного чанка confirm_source_label должен совпадать "
                             f"с его именем «{source.label}».")
        original_label = source.label
        result = {"accepted_copy_key": copy.key, "source_key": source.key,
                  "source_action": action, "label": original_label}
        if action == "archive":
            source.enabled = False
            source.label = f"{original_label} · архив"
            copy.label = original_label
            doc.chunk = copy
            doc.save()
            result["archived_source_label"] = source.label
        else:
            copy.label = original_label
            doc.chunk = copy
            # remove_chunk_quarantined saves the project
            result |= {"removed": remove_chunk_quarantined(doc, source)}
            result["moved_data"] = result["removed"]["moved_data"]
        return result | {"active_chunk_key": doc.chunk.key, "chunk_count": len(doc.chunks),
                         "save_policy": "saved"}
    if method == "chunk_compatibility":
        doc = app.document
        chunks = find_chunks(doc, request["chunk_keys"])
        return {"reference_chunk_key": chunks[0].key,
                "chunks": [chunk_summary_row(chunk) for chunk in chunks],
                "pairs": [pair_compatibility(chunks[0], chunk) for chunk in chunks[1:]]}
    if method == "chunk_duplicate":
        doc = app.document
        ensure_writable(doc)
        source = find_chunk(doc, request["chunk_key"])
        items = [metashape_constant("DataSource", CHUNK_COPY_ITEMS[name]) for name in request["items"]]
        before = {chunk.key for chunk in doc.chunks}
        copy = source.copy(items=items, keypoints=bool(request["keypoints"]))
        if copy is None or copy.key in before:
            raise RuntimeError("Metashape не создала отдельный чанк.")
        copy.label = request["label"]
        return {"source_chunk_key": source.key, "chunk": chunk_summary_row(copy),
                "copied_items": request["items"], "keypoints": bool(request["keypoints"]),
                "save_policy": "manual"}
    if method in ("chunk_alignment_prepare", "chunk_alignment_apply"):
        doc = app.document
        parameters = request["parameters"]
        keys = request["chunk_keys"] if "chunk_keys" in request else parameters["chunk_keys"]
        reference = find_chunk(doc, request["chunk_key"])
        if reference.key in keys:
            raise ValueError("Опорный чанк не должен входить в список выравниваемых.")
        chunks = [reference] + find_chunks(doc, keys)
        method_name = parameters["method"]
        pairs = [pair_compatibility(reference, chunk) for chunk in chunks[1:]]
        blocked = [f"{pair['label']}: {pair['methods'][method_name]['reason']}"
                   for pair in pairs if not pair["methods"][method_name]["possible"]]
        if blocked:
            raise ValueError("Метод недоступен: " + "; ".join(blocked))
        marker_keys = None
        if method_name == "markers":
            shared = set.intersection(*(set(pair["methods"]["markers"]["shared_markers"]) for pair in pairs))
            marker_keys = sorted(marker.key for marker in reference.markers if marker.label in shared)
        if method == "chunk_alignment_prepare":
            summary = {"reference": chunk_summary_row(reference),
                       "aligned_chunks": [chunk_summary_row(chunk) for chunk in chunks[1:]],
                       "pairs": pairs, "method": method_name, "marker_keys": marker_keys,
                       "changes": "меняется только положение (transform) выравниваемых чанков"}
            return chunk_plan(doc, chunks, summary)
        ensure_writable(doc)
        if chunks_fingerprint(doc, chunks) != request["fingerprint"]:
            raise ValueError("Чанки изменились после подготовки плана. Подготовьте новый план.")
        before = {chunk.key: matrix_values(chunk.transform.matrix) for chunk in chunks}
        options = {}
        if marker_keys:
            options["markers"] = marker_keys
        # Metashape expects the reference inside the list too ("Operation not applicable" otherwise).
        doc.alignChunks(progress=TRACKER, chunks=[chunk.key for chunk in chunks], reference=reference.key,
                        method=CHUNK_ALIGNMENT_METHODS[method_name], fit_scale=bool(parameters["fit_scale"]),
                        downscale=int(parameters["downscale"]),
                        generic_preselection=bool(parameters["generic_preselection"]),
                        filter_mask=bool(parameters["filter_mask"]),
                        keypoint_limit=int(parameters["keypoint_limit"]), **options)
        results = []
        for chunk in chunks:
            after = matrix_values(chunk.transform.matrix)
            change = (max(abs(a - b) for a, b in zip(after, before[chunk.key]))
                      if after is not None and before[chunk.key] is not None else None)
            results.append({"chunk_key": chunk.key, "label": chunk.label,
                            "reference": chunk.key == reference.key,
                            # tiny float noise (~1e-15) on the reference is not a movement
                            "transform_changed": (after != before[chunk.key] if change is None
                                                  else change > 1e-9),
                            "max_matrix_change": change,
                            "had_transform_before": before[chunk.key] is not None,
                            "scale": float(chunk.transform.scale) if chunk.transform.scale else None})
        return {"reference_chunk_key": reference.key, "method": method_name, "chunks": results,
                "moved_chunks": sum(row["transform_changed"] for row in results if not row["reference"]),
                "save_policy": "manual"}
    if method in ("chunk_merge_prepare", "chunk_merge_apply"):
        doc = app.document
        parameters = request["parameters"]
        keys = request.get("chunk_keys") or parameters["chunk_keys"]
        chunks = find_chunks(doc, keys)
        if len(chunks) < 2:
            raise ValueError("Для объединения нужно не меньше двух чанков.")
        if method == "chunk_merge_prepare":
            reference = chunks[0]
            summary = {"chunks": [chunk_summary_row(chunk) for chunk in chunks],
                       "pairs": [{"chunk_key": chunk.key, "same_coordinate_system":
                                  coordinate_system_summary(chunk.crs) == coordinate_system_summary(reference.crs)}
                                 for chunk in chunks[1:]],
                       "options": parameters,
                       "changes": "создаётся новый чанк; исходные чанки не меняются"}
            return chunk_plan(doc, chunks, summary)
        ensure_writable(doc)
        if chunks_fingerprint(doc, chunks) != request["fingerprint"]:
            raise ValueError("Чанки изменились после подготовки плана. Подготовьте новый план.")
        before = {chunk.key for chunk in doc.chunks}
        doc.mergeChunks(progress=TRACKER, chunks=[chunk.key for chunk in chunks],
                        **{name: bool(parameters[name]) for name in MERGE_OPTIONS})
        created = [chunk for chunk in doc.chunks if chunk.key not in before]
        if len(created) != 1:
            raise RuntimeError(f"Ожидался один новый чанк, появилось {len(created)}.")
        merged = created[0]
        merged.label = request["copy_label"]
        return {"chunk_key_created": merged.key, "merged_chunk": chunk_summary_row(merged),
                "source_chunk_keys": keys, "source_chunks_unchanged": True, "save_policy": "manual"}
    if method == "get_model_confidence":
        chunk = find_chunk(app.document, request["chunk_key"])
        model = chunk.model if request.get("model_key") is None else next(
            (item for item in chunk.models if item.key == request["model_key"]), None)
        if model is None:
            raise ValueError("Нет модели (активной или с указанным ключом).")
        values = [float(vertex.confidence) for vertex in model.vertices]
        positive = [value for value in values if value > 0]
        if not positive:
            return {"chunk_key": chunk.key, "model_key": model.key, "vertices": len(values),
                    "has_confidence": False,
                    "note": "У вершин нет уверенности: постройте модель из карт глубины с vertex_confidence=true."}
        top = max(positive)
        bins = int(request.get("bins") or 10)
        counts = [0] * bins
        for value in values:
            counts[min(bins - 1, int(bins * value / top))] += 1
        ordered = sorted(values)
        return {"chunk_key": chunk.key, "model_key": model.key, "vertices": len(values), "has_confidence": True,
                "max": top, "median": ordered[len(ordered) // 2],
                "histogram_percent_of_max": [{"from": round(100 * i / bins), "to": round(100 * (i + 1) / bins),
                                              "vertices": count} for i, count in enumerate(counts)],
                "note": "Уверенность — сколько карт глубины подтверждают вершину; в Clean Model Metashape "
                        "показывает её в процентах от максимума."}
    if method == "get_model_statistics":
        chunk = find_chunk(app.document, request["chunk_key"])
        models = list(chunk.models)
        if request.get("model_key") is not None:
            models = [model for model in models if model.key == request["model_key"]]
            if not models:
                raise ValueError("Модель с указанным ключом не найдена.")
        active = chunk.model.key if chunk.model is not None else None
        scale = chunk.transform.scale
        basis = measurement_basis(
            scale, coordinate_system_summary(chunk.crs), chunk.elevation is not None,
            dem_required=False)
        return {"chunk_key": chunk.key, "chunk_scale": optional_float(scale),
                "measurement_basis": basis,
                "active_model_key": active, "model_count": len(chunk.models),
                "models": [{**model_statistics(model), "active": model.key == active} for model in models]}
    if method == "shapes_target":
        chunk = find_chunk(app.document, request["chunk_key"])
        return {"chunk_key": chunk.key, "shape_count": shape_count(chunk),
                "coordinate_system": coordinate_system_summary(chunk.crs)}
    if method == "shapes_import":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        before = shape_count(chunk)
        if before != request["expected_shapes"]:
            raise ValueError("Число фигур в чанке изменилось после проверки. Создайте новый preview.")
        require_geographic(chunk, request["format"])
        boundary = getattr(Metashape.Shape.BoundaryType, BOUNDARY_NAMES[request["boundary_type"]])
        chunk.importShapes(path=request["path"], replace=False, boundary_type=boundary,
                           format=shapes_format(request["format"]))
        after = shape_count(chunk)
        if after <= before:
            raise RuntimeError("Metashape не добавила ни одной фигуры.")
        return {"chunk_key": chunk.key, "shapes_before": before, "added_shapes": after - before,
                "shape_count": after, "boundary_type": request["boundary_type"], "save_policy": "manual"}
    if method == "get_region":
        return region_summary(find_chunk(app.document, request["chunk_key"]))
    if method in ("set_region", "reset_region"):
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        before = region_summary(chunk)
        if method == "reset_region":
            chunk.resetRegion()
        else:
            region = chunk.region
            if request.get("center") is not None:
                region.center = Metashape.Vector(request["center"])
            if request.get("size") is not None:
                region.size = Metashape.Vector(request["size"])
            if request.get("rotation") is not None:
                values = request["rotation"]
                region.rot = Metashape.Matrix([values[0:3], values[3:6], values[6:9]])
            chunk.region = region
        return {"before": before, "after": region_summary(chunk), "save_policy": "manual"}
    if method == "add_marker_projections":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        label = request["label"].strip()
        if any(marker.label == label for marker in chunk.markers):
            raise ValueError(f"Маркер «{label}» уже есть в чанке; выберите другое имя.")
        cameras = {camera.key: camera for camera in chunk.cameras}
        checked = []
        for item in request["projections"]:
            camera = cameras.get(item["camera_key"])
            if camera is None:
                raise ValueError(f"Камера {item['camera_key']} не найдена.")
            if camera.transform is None:
                raise ValueError(f"Камера {camera.label} не выровнена.")
            width, height = camera.sensor.width, camera.sensor.height
            if not (0 <= item["x"] < width and 0 <= item["y"] < height):
                raise ValueError(f"Точка ({item['x']}, {item['y']}) вне снимка {camera.label} {width}x{height}.")
            checked.append((camera, item))
        marker = chunk.addMarker()
        marker.label = label
        for camera, item in checked:
            marker.projections[camera] = Metashape.Marker.Projection(
                Metashape.Vector([item["x"], item["y"]]), True)
        per_camera = []
        for camera, item in checked:
            projected = camera.project(marker.position) if marker.position is not None else None
            error = (math.hypot(projected.x - item["x"], projected.y - item["y"])
                     if projected is not None else None)
            per_camera.append({"camera_key": camera.key, "label": camera.label, "error_px": error})
        return {"chunk_key": chunk.key, "marker": marker_summary(marker, chunk), "projections": per_camera,
                "note": "Большая ошибка на одном снимке значит, что точка указана не в том месте.",
                "save_policy": "manual"}
    if method == "compare_chunk_markers":
        doc = app.document
        reference = find_chunk(doc, request["reference_chunk_key"])
        other = find_chunk(doc, request["chunk_key"])
        labels = request.get("labels")

        def world(chunk):
            matrix = chunk.transform.matrix
            result = {}
            for marker in chunk.markers:
                if marker.position is None or matrix is None:
                    continue
                if labels and marker.label not in labels:
                    continue
                result[marker.label] = matrix.mulp(marker.position)
            return result
        a, b = world(reference), world(other)
        rows = []
        for label in sorted(set(a) & set(b)):
            delta = b[label] - a[label]
            rows.append({"label": label, "distance": float(delta.norm()), "delta": vector_values(delta)})
        distances = [row["distance"] for row in rows]
        return {"reference_chunk_key": reference.key, "chunk_key": other.key, "markers": rows,
                "rms": math.sqrt(sum(d * d for d in distances) / len(distances)) if distances else None,
                "max": max(distances) if distances else None,
                "units": "координаты проекта (метры, если задан масштаб)",
                "only_in_reference": sorted(set(a) - set(b)), "only_in_chunk": sorted(set(b) - set(a))}
    if method == "get_reference_summary":
        return reference_summary(find_chunk(app.document, request["chunk_key"]))
    if method == "get_reference_error_statistics":
        return reference_error_statistics(find_chunk(app.document, request["chunk_key"]))
    if method == "list_camera_references":
        chunk = find_chunk(app.document, request["chunk_key"])
        cameras = [camera for camera in chunk.cameras
                   if camera.type == Metashape.Camera.Type.Regular]
        return {"chunk_key": chunk.key,
                **page(cameras, request, "cameras", camera_reference_summary)}
    if method == "reference_targets":
        chunk = find_chunk(app.document, request["chunk_key"])
        regular = [camera for camera in chunk.cameras
                   if camera.type == Metashape.Camera.Type.Regular]
        return {
            "chunk_key": chunk.key,
            "camera_labels": [Path(camera.photo.path).name
                              if camera.photo is not None and camera.photo.path else camera.label
                              for camera in regular],
            "camera_display_labels": [camera.label for camera in regular],
            "marker_labels": [marker.label for marker in chunk.markers],
        }
    if method == "reference_import":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        before_markers = {marker.key for marker in chunk.markers}
        arguments = {
            "path": request["path"], "format": Metashape.ReferenceFormatCSV,
            "columns": request["columns"], "delimiter": request["delimiter"],
            "skip_rows": request["skip_rows"],
            "items": (Metashape.ReferenceItemsCameras
                      if request["items"] == "cameras" else Metashape.ReferenceItemsMarkers),
            "create_markers": request["create_markers"],
        }
        if request["epsg"] is not None:
            crs = epsg_crs(request["epsg"])
            if not crs.name:
                raise ValueError("Metashape не распознала указанный код EPSG.")
            chunk.crs = crs
            chunk.camera_crs = crs
            chunk.marker_crs = crs
            arguments["crs"] = crs
        chunk.importReference(**arguments)
        enabled = any(camera.reference.location_enabled and camera.reference.location is not None
                      for camera in chunk.cameras) or any(
            marker.reference.enabled and marker.reference.location is not None
            for marker in chunk.markers)
        if enabled:
            chunk.updateTransform()
        added = [{"key": marker.key, "label": marker.label} for marker in chunk.markers
                 if marker.key not in before_markers]
        return {**reference_summary(chunk), "added_markers": added,
                "transform_updated": enabled, "save_policy": "manual"}
    if method == "asset_export":
        chunk = find_chunk(app.document, request["chunk_key"])
        folder = Path(tempfile.mkdtemp(prefix=f"metashape-{request['kind'].replace('_', '-')}-",
                                       dir=request["directory"]))
        try:
            details = export_asset(chunk, request, folder)
            files = output_files(folder)
        except Exception:
            # macOS AppleDouble files (._*) on external volumes can vanish mid-removal.
            shutil.rmtree(folder, ignore_errors=True)
            raise
        boundaries = boundary_shape_count(chunk) if request["kind"] in (
            "model", "point_cloud", "tiled_model") else 0
        return {"chunk_key": chunk.key, "kind": request["kind"], "format": request["options"].get("format"),
                "output_directory": str(folder), "files": files, **details,
                "boundary_shapes": boundaries,
                "clipped_to_boundary": bool(boundaries and request["options"].get("clip_to_boundary"))}
    if method == "export_reference":
        chunk = find_chunk(app.document, request["chunk_key"])
        items = {
            "cameras": Metashape.ReferenceItemsCameras,
            "markers": Metashape.ReferenceItemsMarkers,
        }[request["items"]]
        folder = Path(tempfile.mkdtemp(prefix="metashape-reference-",
                                       dir=request["directory"]))
        path = folder / "reference.csv"
        try:
            chunk.exportReference(str(path), format=Metashape.ReferenceFormatCSV,
                                  items=items, columns=request["columns"],
                                  delimiter=request["delimiter"], precision=request["precision"])
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError("Metashape не создала reference CSV.")
        except Exception:
            shutil.rmtree(folder)
            raise
        return {"chunk_key": chunk.key, "path": str(path),
                "output_directory": str(folder), "files": [str(path)]}
    if method == "set_marker_reference":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        marker = next((item for item in chunk.markers if item.key == request["marker_key"]), None)
        if marker is None:
            raise ValueError("Маркер с указанным ключом не найден.")
        marker.reference.location = Metashape.Vector(request["location"])
        marker.reference.accuracy = Metashape.Vector(request["accuracy"])
        marker.reference.enabled = request["enabled"]
        if request["enabled"]:
            chunk.updateTransform()
        return {"chunk_key": chunk.key, "marker": marker_summary(marker, chunk),
                "save_policy": "manual"}
    if method == "set_check_points":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        requested = set(request["marker_keys"])
        selected = [marker for marker in chunk.markers if marker.key in requested]
        missing = sorted(requested - {marker.key for marker in selected})
        if missing:
            raise ValueError(f"Маркеры с ключами не найдены: {missing[:20]}")
        without = [marker.label for marker in selected if marker.reference.location is None]
        if without:
            raise ValueError(f"У маркеров нет опорных координат: {without[:20]}")
        changed = []
        for marker in selected:
            enabled = not request["check"]
            if marker.reference.enabled != enabled:
                marker.reference.enabled = enabled
                changed.append(marker.key)
        if changed:
            chunk.updateTransform()
        return {"chunk_key": chunk.key, "changed_marker_keys": sorted(changed),
                "control_points": sum(m.reference.enabled and m.reference.location is not None
                                      for m in chunk.markers),
                "check_points": sum((not m.reference.enabled) and m.reference.location is not None
                                    for m in chunk.markers),
                "note": "Положение чанка пересчитано по оставшимся опорным точкам (updateTransform); "
                        "для уточнения калибровки запустите оптимизацию камер.",
                "save_policy": "manual"}
    if method == "set_cameras_enabled":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        requested = set(request["camera_keys"])
        selected = [camera for camera in chunk.cameras if camera.key in requested]
        missing = sorted(requested - {camera.key for camera in selected})
        if missing:
            raise ValueError(f"Камеры с ключами не найдены: {missing[:20]}")
        changed = []
        for camera in selected:
            if camera.enabled != request["enabled"]:
                camera.enabled = request["enabled"]
                changed.append(camera.key)
        return {"chunk_key": chunk.key, "requested": len(requested),
                "changed": len(changed), "changed_camera_keys": sorted(changed),
                "enabled_cameras": sum(camera.enabled for camera in chunk.cameras),
                "disabled_cameras": sum(not camera.enabled for camera in chunk.cameras),
                "save_policy": "manual"}
    if method == "set_chunk_coordinate_system":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        crs = epsg_crs(request["epsg"])
        if not crs.name:
            raise ValueError("Metashape не распознала указанный код EPSG.")
        chunk.crs = crs
        if request["apply_to_cameras"]:
            chunk.camera_crs = crs
        if request["apply_to_markers"]:
            chunk.marker_crs = crs
        has_enabled_references = any(
            camera.reference.enabled and camera.reference.location is not None
            for camera in chunk.cameras
        ) or any(
            marker.reference.enabled and marker.reference.location is not None
            for marker in chunk.markers
        )
        if has_enabled_references:
            chunk.updateTransform()
        return {**reference_summary(chunk), "transform_updated": has_enabled_references,
                "save_policy": "manual"}
    if method == "list_sensors":
        chunk = find_chunk(app.document, request["chunk_key"])
        return {"chunk_key": chunk.key,
                **page(chunk.sensors, request, "sensors", lambda sensor: sensor_summary(sensor, chunk))}
    if method == "get_alignment_statistics":
        return alignment_statistics(find_chunk(app.document, request["chunk_key"]))
    if method == "get_alignment_diagnostics":
        return alignment_diagnostics(find_chunk(app.document, request["chunk_key"]))
    if method == "list_markers":
        chunk = find_chunk(app.document, request["chunk_key"])
        return {"chunk_key": chunk.key,
                **page(chunk.markers, request, "markers", lambda marker: marker_summary(marker, chunk))}
    if method == "list_scalebars":
        chunk = find_chunk(app.document, request["chunk_key"])
        return {"chunk_key": chunk.key,
                **page(chunk.scalebars, request, "scalebars",
                       lambda scalebar: scalebar_summary(scalebar, chunk))}
    if method == "create_scalebar":
        doc = app.document
        ensure_writable(doc)
        chunk = find_chunk(doc, request["chunk_key"])
        if request["marker0_key"] == request["marker1_key"]:
            raise ValueError("Для масштабной линейки нужны два разных маркера.")
        marker0 = next((marker for marker in chunk.markers if marker.key == request["marker0_key"]), None)
        marker1 = next((marker for marker in chunk.markers if marker.key == request["marker1_key"]), None)
        if marker0 is None or marker1 is None:
            raise ValueError("Один из маркеров отсутствует в выбранном чанке.")
        requested_pair = {marker0.key, marker1.key}
        for scalebar in chunk.scalebars:
            if (isinstance(scalebar.point0, Metashape.Marker)
                    and isinstance(scalebar.point1, Metashape.Marker)
                    and {scalebar.point0.key, scalebar.point1.key} == requested_pair):
                raise ValueError("Масштабная линейка между этими маркерами уже существует.")
        scalebar = chunk.addScalebar(marker0, marker1)
        scalebar.reference.distance = request["distance"]
        scalebar.reference.accuracy = request["accuracy"]
        scalebar.reference.enabled = True
        return {"chunk_key": chunk.key, "scalebar": scalebar_summary(scalebar, chunk),
                "save_policy": "manual"}
    if method == "get_metashape_log":
        limit = request["limit"]
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Неверное число строк.")
        text = app.console_pane.contents
        tail = text[-32768:].encode("utf-8")[-32768:].decode("utf-8", errors="ignore")
        lines = tail.splitlines()
        return {"source": "Metashape.app.console_pane.contents", "lines": lines[-limit:],
                "truncated": len(tail) < len(text) or len(lines) > limit}
    if method == "quality_sources":
        doc = app.document
        chunk = find_chunk(doc, request["chunk_key"])
        images = []
        for camera in chunk.cameras:
            if not camera.enabled or camera.type != Metashape.Camera.Type.Regular:
                continue
            photo = camera.photo
            if photo is None or not photo.path:
                continue
            path = Path(photo.path)
            if not path.is_absolute():
                path = (Path(doc.path).parent / path).absolute()
            images.append({"key": camera.key, "label": camera.label, "path": str(path), "layer": photo.layer})
        return {"project": Path(doc.path).name if doc.path else None, "chunk_label": chunk.label, "images": images}
    if method == "quality_measure":
        try:
            image = Metashape.Image.open(request["path"], layer=request["layer"])
            if image is None:
                raise ValueError("Metashape не смогла прочитать изображение.")
            score = float(Metashape.Utils.estimateImageQuality(image))
            if not math.isfinite(score):
                raise ValueError("Metashape вернула неконечную оценку.")
            return {"score": score}
        except Exception as error:
            return {"error": str(error) or type(error).__name__}
    raise ValueError("Неизвестная операция.")


class MetashapeService(QtCore.QObject):
    revision = "0.46.0"
    checkpoint_methods = {
        "create_project", "open_project", "save_project", "save_project_as",
        "add_chunk", "add_photos", "create_scalebar", "alignment_copy",
        "reconstruction_copy", "texture_export_copy", "camera_optimization_copy",
        "tie_point_filtering_copy", "sensor_calibration_copy", "camera_reference_exif_copy",
        "reference_crs_transform_copy",
        "point_cloud_cleanup_copy",
        "raster_products_copy", "set_chunk_coordinate_system",
        "reference_import", "export_reference", "set_marker_reference",
        "set_cameras_enabled", "set_check_points", "shapes_import", "set_region", "reset_region", "add_marker_projections", "marker_detection_copy", "marker_refinement_copy",
        "point_cloud_copy", "model_finishing_copy", "model_import_copy", "ground_classification_copy", "contours_copy", "autopilot_copy", "asset_export", "mask_import", "mask_generate", "point_cloud_import", "chunk_duplicate",
        "chunk_alignment_apply", "chunk_merge_apply", "chunk_set_enabled", "chunk_remove", "chunk_promote",
    }

    def __init__(self, application):
        super().__init__(application)
        self.alignment = CameraAlignment()
        self.process = QtCore.QProcess(self)
        self.process.readyReadStandardOutput.connect(self.respond)
        self.process.readyReadStandardError.connect(self.diagnostics)
        self.process.errorOccurred.connect(self.process_error)
        self.process.finished.connect(self.finished)
        application.aboutToQuit.connect(self.stop)
        self.diagnostic = ""
        self.console_position = len(Metashape.app.console_pane.contents)
        self.console_partial = ""
        self.console_context = None
        self.console_timer = QtCore.QTimer(self)
        self.console_timer.setInterval(1000)
        self.console_timer.timeout.connect(self.capture_console)
        self.settings_path = None
        self.restore_points = None

    def start(self):
        if self.process.state() != QtCore.QProcess.NotRunning:
            return
        url = urlsplit(os.environ.get("METASHAPE_MCP_URL", DEFAULT_URL))
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost")
                or not url.port or url.path != "/mcp" or url.query or url.fragment or url.username):
            raise ValueError("Для локального сервера укажите http://127.0.0.1:ПОРТ/mcp.")
        data = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.GenericDataLocation)
        root = Path(data) / "Metashape MCP"
        directory = root / "server" / str(url.port)
        self.settings_path = root / "settings.json"
        self.restore_points = RestorePoints(directory)
        self.diagnostic = ""
        start_sdk_process(self.process, HERE / "metashape_mcp_server.py", [str(url.port), str(directory)])
        self.console_timer.start()

    def send_console_lines(self, lines):
        if not lines or self.process.state() == QtCore.QProcess.NotRunning:
            return
        context = self.console_context or {}
        for start in range(0, len(lines), 100):
            items = []
            for line in lines[start:start + 100]:
                truncated = len(line) > 32768
                items.append({"text": line[-32768:] if truncated else line,
                              "truncated": truncated})
            message = {"event": "metashape_console", "lines": items, **context}
            self.process.write((json.dumps(message, ensure_ascii=True) + "\n").encode("ascii"))

    def schedule_reload(self):
        """Reload this service with the current source after the reply has been sent."""
        if not load_settings(self.settings_path).get("allow_mcp_reload"):
            raise ValueError("Перезагрузка через MCP выключена. Включите её галочкой в окне MCP "
                             "или нажмите «Перезагрузить сервис».")
        job = active_job_id(self.restore_points.directory)
        if job:
            raise ValueError(f"Идёт задание {job}. Перезагрузка после его завершения.")
        launcher = str(HERE / "start_metashape_mcp.py")

        def run():
            try:
                runpy.run_path(launcher, run_name="__main__")
            except Exception as error:
                print("Metashape MCP: перезагрузка не выполнена:", error)

        QtCore.QTimer.singleShot(500, run)
        return {"scheduled": True, "from_revision": self.revision,
                "note": "Сервер перезапустится примерно через секунду; переподключитесь."}

    def script_settings(self):
        return load_settings(self.settings_path)

    def path_policy(self):
        roots = describe_roots(self.script_settings()["allowed_roots"])
        return {"enabled": bool(roots), "roots": roots,
                "all_roots_available": bool(roots) and all(item["exists"] for item in roots)}

    def validate_paths(self, paths):
        roots = self.script_settings()["allowed_roots"]
        resolved = [str(resolve_scoped(value, roots)) for value in paths]
        return {"paths": resolved, "policy": self.path_policy()}

    def script_path(self, relative_path):
        settings = self.script_settings()
        if not settings["allow_user_scripts"]:
            raise ValueError("Запуск пользовательских скриптов выключен в настройках Metashape MCP.")
        root_value = settings["user_scripts_directory"]
        if not root_value:
            raise ValueError("В настройках не выбрана папка пользовательских скриптов.")
        root = Path(root_value).expanduser().resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("Скрипт должен находиться внутри разрешённой папки.") from error
        if not candidate.is_file() or candidate.suffix.lower() != ".py":
            raise ValueError("Пользовательский скрипт .py не найден.")
        return settings, root, candidate

    def list_user_scripts(self):
        settings = self.script_settings()
        result = {"enabled": bool(settings["allow_user_scripts"]),
                  "directory": settings["user_scripts_directory"], "scripts": []}
        if not result["enabled"] or not result["directory"]:
            return result
        root = Path(result["directory"]).expanduser().resolve()
        if not root.is_dir():
            result["error"] = "Разрешённая папка недоступна."
            return result
        approved = settings["approved_script_hashes"]
        for path in sorted(root.rglob("*.py"))[:1000]:
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            result["scripts"].append({"path": relative, "sha256": digest,
                                      "approved": approved.get(relative) == digest})
        return result

    def run_user_script(self, request):
        settings, root, path = self.script_path(request["path"])
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if settings["approved_script_hashes"].get(relative) != digest:
            answer = QtWidgets.QMessageBox.warning(
                None, "Metashape MCP — пользовательский скрипт",
                "Скрипт будет выполнен внутри Metashape с правами текущего пользователя.\n\n"
                f"Файл: {relative}\nSHA-256: {digest}\n\n"
                "Он может необратимо изменить внешние файлы и настройки. "
                "Точка восстановления защищает только проект Metashape.\n\nРазрешить эту версию скрипта?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                raise ValueError("Пользователь не разрешил запуск скрипта.")
            settings["approved_script_hashes"][relative] = digest
            save_settings(self.settings_path, settings)
        document = Metashape.app.document
        if document is None or not document.path or document.read_only:
            raise ValueError("Перед пользовательским скриптом нужен сохранённый доступный для записи проект.")
        answer = QtWidgets.QMessageBox.question(
            None, "Metashape MCP — сохранение проекта",
            "Перед запуском пользовательского скрипта сохранить текущий проект?\n\n"
            "Без сохранения запуск будет отменён.",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Save:
            raise ValueError("Пользователь отменил запуск до сохранения проекта.")
        document.save()
        namespace = runpy.run_path(
            str(path), init_globals={"MCP_ARGUMENTS": request.get("arguments") or {}}
        )
        result = namespace.get("MCP_RESULT")
        try:
            json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("MCP_RESULT скрипта должен быть совместим с JSON.") from error
        return {"path": relative, "sha256": digest, "result": result,
                "external_changes_reversible": False}

    def capture_console(self, flush=False):
        try:
            text = Metashape.app.console_pane.contents
            if len(text) < self.console_position:
                self.console_position = 0
                self.console_partial = ""
            appended = text[self.console_position:]
            self.console_position = len(text)
            combined = self.console_partial + appended
            parts = combined.split("\n")
            if flush or combined.endswith("\n"):
                lines = parts[:-1] if combined.endswith("\n") else parts
                self.console_partial = ""
            else:
                lines = parts[:-1]
                self.console_partial = parts[-1]
            self.send_console_lines([line.rstrip("\r") for line in lines if line.rstrip("\r")])
        except Exception as error:
            self.diagnostic = (self.diagnostic + "\nConsole capture: "
                               + (str(error) or type(error).__name__))[-3000:]

    # Metashape keeps processing Qt events during long API calls, so respond() can be re-entered
    # while another request is still running. Only these may run in the middle of another call.
    reentrant_safe = {"get_service_status", "get_metashape_log", "path_policy", "progress_cancel"}

    def respond(self):
        while self.process.canReadLine():
            request = json.loads(bytes(self.process.readLine()))
            reply = {"id": request["id"]}
            busy = getattr(self, "busy_method", None)
            if busy is not None and request.get("method") not in self.reentrant_safe:
                reply["error"] = (f"Metashape ещё выполняет {busy}. Повторите запрос после его "
                                  "завершения.")
                self.process.write((json.dumps(reply, ensure_ascii=True) + "\n").encode("ascii"))
                continue
            outer_busy = busy
            if request.get("method") not in self.reentrant_safe:
                self.busy_method = request.get("method")
            previous_context = self.console_context
            self.console_context = {
                key: value for key, value in (
                    ("job_id", request.get("job_id")), ("operation", request.get("method")))
                if value is not None
            }
            restore_point = None
            try:
                method = request["method"]
                if method in self.checkpoint_methods:
                    document = Metashape.app.document
                    placeholder = (document is not None and document.path
                                   and Path(document.path).name.startswith("restore-placeholder-")
                                   and Path(document.path).resolve().parent
                                   == self.restore_points.directory.resolve())
                    if placeholder and method not in ("create_project", "open_project"):
                        raise ValueError("Открыт служебный пустой проект (откат был прерван). "
                                         "Откройте рабочий проект через open_project.")
                    if placeholder:
                        pass  # empty service project left by an interrupted restore: nothing to protect
                    elif document is not None and document.path:
                        self.validate_paths([document.path])
                    elif method not in ("create_project", "open_project"):
                        raise ValueError("Для изменяющей операции нужен сохранённый проект.")
                    if placeholder or (method in ("create_project", "open_project")
                                       and document is not None and document.read_only):
                        pass  # a read-only project cannot change, nothing to protect
                    else:
                        restore_point = self.restore_points.create(
                            "before:" + method,
                            save_current=method not in (
                                "save_project", "create_project", "open_project", "save_project_as"),
                            full=False)
                if method == "restore_point_create":
                    result = self.restore_points.create(request["label"], save_current=True)
                elif method == "restore_points_list":
                    result = self.restore_points.list(request["offset"], request["limit"])
                elif method == "restore_point_restore":
                    result = self.restore_points.restore(request["restore_point_id"])
                elif method == "restore_point_register_outputs":
                    result = self.restore_points.register_outputs(
                        request["restore_point_id"], request["paths"])
                elif method == "quarantined_outputs_list":
                    result = self.restore_points.list_quarantined(
                        request["offset"], request["limit"])
                elif method == "quarantined_output_restore":
                    result = self.restore_points.restore_quarantined(request["quarantine_id"])
                elif method == "service_reload":
                    result = self.schedule_reload()
                elif method == "path_policy":
                    result = self.path_policy()
                elif method == "paths_validate":
                    result = self.validate_paths(request["paths"])
                elif method == "user_scripts_list":
                    result = self.list_user_scripts()
                elif method == "user_script_run":
                    result = self.run_user_script(request)
                elif method in ("alignment_prepare", "reconstruction_prepare", "texture_export_prepare",
                              "camera_optimization_prepare", "tie_point_filtering_prepare",
                              "sensor_calibration_prepare", "camera_reference_exif_prepare",
                              "reference_crs_transform_prepare",
                              "point_cloud_cleanup_prepare",
                              "raster_products_prepare",
                              "marker_detection_prepare", "marker_refinement_prepare",
                              "point_cloud_prepare", "model_finishing_prepare", "model_import_prepare",
                              "ground_classification_prepare", "contours_prepare", "autopilot_prepare"):
                    # The same parameter dict must reach snapshot() at prepare and at copy time,
                    # otherwise parameter-dependent fingerprints differ.
                    result = self.alignment.prepare(
                        request["chunk_key"], method.removesuffix("_prepare"),
                        request.get("parameters", request))
                elif method in ("alignment_copy", "reconstruction_copy", "texture_export_copy",
                                "camera_optimization_copy", "tie_point_filtering_copy",
                                "sensor_calibration_copy", "camera_reference_exif_copy",
                                "reference_crs_transform_copy",
                                "point_cloud_cleanup_copy",
                                "raster_products_copy",
                                "marker_detection_copy", "marker_refinement_copy",
                                "point_cloud_copy", "model_finishing_copy", "model_import_copy",
                                "ground_classification_copy", "contours_copy", "autopilot_copy"):
                    result = self.alignment.duplicate(request)
                elif method in ("alignment_match", "alignment_align", "reconstruction_depth_maps", "reconstruction_model",
                                "texture_export_uv", "texture_export_texture", "texture_export_export",
                                "camera_optimization_optimize", "raster_products_dem",
                                "raster_products_orthomosaic", "raster_products_export",
                                "marker_detection_detect", "marker_refinement_refine",
                                "point_cloud_build", "model_finishing_finish", "model_import_import",
                                "ground_classification_classify", "contours_contours", "contours_export") \
                        or method in ("sensor_calibration_apply", "camera_reference_exif_apply",
                                     "reference_crs_transform_apply",
                                     "point_cloud_cleanup_apply") \
                        or method.startswith(("autopilot_step", "tie_point_filtering_pass")):
                    TRACKER.begin(request.get("job_id"), method)
                    try:
                        result = self.alignment.execute(request)
                    finally:
                        TRACKER.finish()
                elif method in TRACKED_HOST_METHODS:
                    TRACKER.begin(request.get("job_id"), method)
                    try:
                        result = read_host(request)
                    finally:
                        TRACKER.finish()
                else:
                    result = read_host(request)
                if restore_point is not None and isinstance(result, dict):
                    if result.get("moved_data"):
                        self.restore_points.record_moved_data(
                            restore_point["restore_point_id"], result["moved_data"])
                    result["restore_point"] = restore_point

                reply["result"] = result
            except Exception as error:
                reply["error"] = str(error) or type(error).__name__
                if not isinstance(error, ValueError):
                    # unexpected errors: name our own code location to make them diagnosable
                    frames = [frame for frame in traceback.extract_tb(error.__traceback__)
                              if Path(frame.filename).resolve().parent == HERE]
                    if frames:
                        reply["error"] += f" [{Path(frames[-1].filename).name}:{frames[-1].lineno}]"
                if restore_point is not None:
                    reply["error"] += (f" (до ошибки создана точка восстановления "
                                       f"{restore_point['restore_point_id']})")
            finally:
                self.busy_method = outer_busy
                self.capture_console()
                self.console_context = previous_context
            self.process.write((json.dumps(reply, ensure_ascii=True) + "\n").encode("ascii"))

    def diagnostics(self):
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self.diagnostic = (self.diagnostic + text)[-3000:]

    def process_error(self, error):
        if error == QtCore.QProcess.FailedToStart:
            print("Metashape MCP:", self.process.errorString())

    def finished(self, code, *_):
        self.diagnostics()
        if code:
            print("Metashape MCP:", self.diagnostic.strip() or f"Сервер завершился с кодом {code}.")

    def stop(self):
        self.console_timer.stop()
        self.capture_console(flush=True)
        self.process.waitForBytesWritten(500)
        self.process.closeWriteChannel()
        if self.process.state() != QtCore.QProcess.NotRunning:
            self.process.waitForFinished(1500)


def active_job_id(directory):
    """Return the ID of a running or cancelling job from the service database, if any."""
    database = Path(directory) / "quality.sqlite3"
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
    try:
        row = connection.execute(
            "SELECT id FROM jobs WHERE state IN ('running','cancelling') LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return row[0] if row else None


def start():
    """Start the service, or reload a running one with the current source code."""
    application = QtWidgets.QApplication.instance()
    service = getattr(application, "_metashape_read_service", None)
    if service is not None:
        running = service.process.state() != QtCore.QProcess.NotRunning
        directory = getattr(getattr(service, "restore_points", None), "directory", None)
        if running and directory is not None:
            job = active_job_id(directory)
            if job:
                raise RuntimeError(
                    f"Идёт задание {job}. Перезагрузите сервис после его завершения или отмены.")
        service.stop()
        if service.process.state() != QtCore.QProcess.NotRunning:
            service.process.waitForFinished(5000)
        if service.process.state() != QtCore.QProcess.NotRunning:
            service.process.kill()
            service.process.waitForFinished(2000)
        if service.process.state() != QtCore.QProcess.NotRunning:
            raise RuntimeError("Предыдущая версия сервера ещё завершает работу. Повторите запуск.")
        service.deleteLater()
        print(f"Metashape MCP: перезагрузка сервиса до версии {MetashapeService.revision}.")
    monitor = getattr(application, "_metashape_mcp_monitor", None)
    if monitor is not None and not getattr(monitor, "closing", False):
        monitor.close()
    application._metashape_mcp_monitor = None
    service = MetashapeService(application)
    application._metashape_read_service = service
    service.start()
    show_service_monitor()


if __name__ == "__main__":
    start()
