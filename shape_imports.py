"""Previews for importing GIS shapes (SHP, DXF, GeoJSON, KML) into a chunk.

Import always uses replace=False so existing shapes stay. A shapefile is fingerprinted
together with its sidecar files, because Metashape reads them too.
"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

FORMATS = {".shp": "shp", ".dxf": "dxf", ".geojson": "geojson", ".json": "geojson", ".kml": "kml"}
SIDECARS = (".shx", ".dbf", ".prj", ".cpg")
BOUNDARY_TYPES = ("none", "outer", "inner")


def related_files(path):
    files = [path]
    if path.suffix.lower() == ".shp":
        for suffix in SIDECARS:
            for candidate in (path.with_suffix(suffix), path.with_suffix(suffix.upper())):
                if candidate.is_file():
                    files.append(candidate)
                    break
        if not any(f.suffix.lower() == ".shx" for f in files) or not any(f.suffix.lower() == ".dbf" for f in files):
            raise ValueError("Для shapefile нужны файлы .shx и .dbf рядом с .shp.")
    return files


def fingerprint(files):
    result = []
    for path in files:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= 256 * 1024 * 1024 else None
        result.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest})
    return result


class ShapeImports:
    def __init__(self, directory, bridge, record):
        self.directory = Path(directory) / "shape-import-previews"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.record = record

    def _path(self, preview_id):
        if not isinstance(preview_id, str) or len(preview_id) != 32:
            raise ValueError("Неверный ID предварительной проверки фигур.")
        return self.directory / f"{preview_id}.json"

    def load(self, preview_id):
        path = self._path(preview_id)
        if not path.is_file():
            raise ValueError("Предварительная проверка фигур не найдена.")
        return json.loads(path.read_text(encoding="utf-8"))

    async def prepare(self, chunk_key, path, boundary_type="none"):
        path = Path(path).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Укажите абсолютный путь к существующему файлу фигур.")
        kind = FORMATS.get(path.suffix.lower())
        if kind is None:
            raise ValueError("Поддерживаются SHP, DXF, GeoJSON и KML.")
        if boundary_type not in BOUNDARY_TYPES:
            raise ValueError("boundary_type: none, outer или inner.")
        files = related_files(path.resolve())
        target = await self.bridge.call("shapes_target", timeout=None, chunk_key=chunk_key)
        preview_id = uuid4().hex
        preview = {"preview_id": preview_id, "chunk_key": chunk_key, "path": str(path.resolve()),
                   "format": kind, "boundary_type": boundary_type, "files": fingerprint(files),
                   "existing_shapes": target["shape_count"],
                   "chunk_coordinate_system": target["coordinate_system"],
                   "notes": ["Существующие фигуры сохраняются (replace=false)."] +
                            (["Граница outer/inner обрежет экспорт моделей и облаков (clip_to_boundary)."]
                             if boundary_type != "none" else [])}
        self._path(preview_id).write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
        self.record("shapes_import_prepared", preview_id=preview_id, chunk_key=chunk_key, path=preview["path"])
        return preview

    async def apply(self, preview_id):
        preview = self.load(preview_id)
        files = [Path(item["path"]) for item in preview["files"]]
        if not all(path.is_file() for path in files) or fingerprint(files) != preview["files"]:
            raise ValueError("Файлы фигур изменились после проверки. Создайте новый preview.")
        result = await self.bridge.call("shapes_import", timeout=None, chunk_key=preview["chunk_key"],
                                        path=preview["path"], format=preview["format"],
                                        boundary_type=preview["boundary_type"],
                                        expected_shapes=preview["existing_shapes"])
        self.record("shapes_import_completed", preview_id=preview_id, chunk_key=preview["chunk_key"],
                    added=result.get("added_shapes"))
        return {"preview_id": preview_id, **result}
