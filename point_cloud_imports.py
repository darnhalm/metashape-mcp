"""Persistent previews for importing LAS, LAZ, E57 and PLY point clouds into Metashape.

Imports run through ``Chunk.importPointCloud(replace_asset=False)`` so existing
clouds stay untouched; the host verifies that exactly one new non-empty cloud
appeared. Point counts are read from LAS/LAZ and PLY headers when possible.
"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4


FORMATS = {".las": "PointCloudFormatLAS", ".laz": "PointCloudFormatLAZ",
           ".e57": "PointCloudFormatE57", ".ply": "PointCloudFormatPLY"}
HEAD_BYTES = 64 * 1024


def las_point_count(path):
    """Number of point records from a LAS/LAZ public header block (1.0-1.4)."""
    with Path(path).open("rb") as stream:
        header = stream.read(375)
    if len(header) < 227 or header[:4] != b"LASF":
        raise ValueError("Файл не похож на LAS/LAZ: нет сигнатуры LASF.")
    major, minor = header[24], header[25]
    legacy = int.from_bytes(header[107:111], "little")
    if (major, minor) >= (1, 4) and len(header) >= 255:
        extended = int.from_bytes(header[247:255], "little")
        return extended or legacy, f"{major}.{minor}"
    return legacy, f"{major}.{minor}"


def ply_point_count(path):
    """Vertex count from a PLY header (ASCII or binary)."""
    with Path(path).open("rb") as stream:
        if stream.readline().strip() != b"ply":
            raise ValueError("Файл не похож на PLY: нет строки ply в начале.")
        count, encoding = None, None
        for _ in range(200):
            line = stream.readline()
            if not line:
                break
            words = line.decode("ascii", errors="replace").split()
            if words[:1] == ["format"] and len(words) > 1:
                encoding = words[1]
            if words[:2] == ["element", "vertex"] and len(words) > 2:
                count = int(words[2])
            if words[:1] == ["end_header"]:
                return count, encoding
    raise ValueError("Заголовок PLY не завершён строкой end_header.")


def fingerprint(path):
    path = Path(path)
    stat = path.stat()
    with path.open("rb") as stream:
        head = hashlib.sha256(stream.read(HEAD_BYTES)).hexdigest()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "head_sha256": head}


def inspect_file(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Укажите абсолютный путь к существующему файлу облака точек.")
    suffix = path.suffix.lower()
    if suffix not in FORMATS:
        raise ValueError("Поддерживаются форматы LAS, LAZ, E57 и PLY.")
    info = {"path": str(path.resolve()), "format": suffix[1:], "header_point_count": None}
    if suffix in (".las", ".laz"):
        info["header_point_count"], info["las_version"] = las_point_count(path)
    elif suffix == ".ply":
        info["header_point_count"], info["ply_encoding"] = ply_point_count(path)
    if info["header_point_count"] == 0:
        raise ValueError("В заголовке файла указано 0 точек.")
    return info


class PointCloudImports:
    def __init__(self, directory, bridge, record):
        self.directory = Path(directory) / "point-cloud-import-previews"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.record = record

    def _path(self, preview_id):
        if not isinstance(preview_id, str) or len(preview_id) != 32:
            raise ValueError("Неверный ID предварительной проверки облака точек.")
        return self.directory / f"{preview_id}.json"

    def load(self, preview_id):
        path = self._path(preview_id)
        if not path.is_file():
            raise ValueError("Предварительная проверка облака точек не найдена.")
        return json.loads(path.read_text(encoding="utf-8"))

    async def prepare(self, chunk_key, path, epsg=None, load_colors=True, calculate_normals=True):
        if epsg is not None and (not isinstance(epsg, int) or not 1 <= epsg <= 999999):
            raise ValueError("Неверный код EPSG.")
        info = inspect_file(path)
        target = await self.bridge.call("point_cloud_import_target", timeout=None,
                                        chunk_key=chunk_key, epsg=epsg)
        preview_id = uuid4().hex
        preview = {
            "preview_id": preview_id, "chunk_key": chunk_key, **info,
            "fingerprint": fingerprint(info["path"]), "epsg": epsg,
            "load_colors": bool(load_colors), "calculate_normals": bool(calculate_normals),
            "existing_point_clouds": target["point_clouds"],
            "active_point_cloud_key": target["active_point_cloud_key"],
            "chunk_coordinate_system": target["coordinate_system"],
            "epsg_name": target.get("epsg_name"),
            "notes": ([] if epsg is not None else
                      ["EPSG не указан: Metashape возьмёт систему координат из файла или чанка."]),
        }
        self._path(preview_id).write_text(
            json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.record("point_cloud_import_prepared", preview_id=preview_id, chunk_key=chunk_key,
                    path=info["path"], header_point_count=info["header_point_count"])
        return preview

    async def apply(self, preview_id, job_id=None):
        preview = self.load(preview_id)
        path = Path(preview["path"])
        if not path.is_file() or fingerprint(path) != preview["fingerprint"]:
            raise ValueError("Файл облака точек изменился после проверки. Создайте новый preview.")
        result = await self.bridge.call(
            "point_cloud_import", timeout=None, job_id=job_id, chunk_key=preview["chunk_key"], path=preview["path"],
            format=preview["format"], epsg=preview["epsg"], load_colors=preview["load_colors"],
            calculate_normals=preview["calculate_normals"],
            expected_existing=[item["key"] for item in preview["existing_point_clouds"]])
        expected = preview["header_point_count"]
        result["header_point_count"] = expected
        result["point_count_matches_header"] = (
            None if expected is None else result["imported_point_count"] == expected)
        self.record("point_cloud_import_completed", preview_id=preview_id,
                    chunk_key=preview["chunk_key"], point_cloud_key=result["point_cloud_key"],
                    points=result["imported_point_count"])
        return {"preview_id": preview_id, **result}
