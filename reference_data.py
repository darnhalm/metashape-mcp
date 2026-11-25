"""Persistent, file-fingerprinted previews for Metashape reference CSV imports."""

import csv
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4


CSV_COLUMNS = frozenset("noxyzXYZabcABC")
NUMERIC_COLUMNS = frozenset("xyzXYZabcABC")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ReferenceImports:
    def __init__(self, directory, bridge, record):
        self.directory = Path(directory) / "reference-previews"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.record = record

    def _path(self, preview_id):
        if not isinstance(preview_id, str) or len(preview_id) != 32:
            raise ValueError("Неверный ID предварительной проверки.")
        return self.directory / f"{preview_id}.json"

    def _load(self, preview_id):
        path = self._path(preview_id)
        if not path.is_file():
            raise ValueError("Предварительная проверка reference-файла не найдена.")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def validate_parameters(path, columns, delimiter, skip_rows, epsg):
        path = Path(path).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Укажите абсолютный путь к существующему CSV-файлу.")
        if path.suffix.lower() not in (".csv", ".txt"):
            raise ValueError("Предварительная проверка поддерживает CSV или TXT.")
        if not columns or "n" not in columns or any(value not in CSV_COLUMNS for value in columns):
            raise ValueError("columns должен содержать n и только noxyzXYZabcABC без группировок.")
        if len(set(columns)) != len(columns):
            raise ValueError("Каждый код столбца в columns можно использовать только один раз.")
        if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n":
            raise ValueError("Разделитель CSV должен быть одним печатным символом.")
        if not isinstance(skip_rows, int) or not 0 <= skip_rows <= 1000:
            raise ValueError("skip_rows должен находиться в диапазоне от 0 до 1000.")
        if epsg is not None and (not isinstance(epsg, int) or not 1 <= epsg <= 999999):
            raise ValueError("Неверный код EPSG.")
        return path.resolve()

    @staticmethod
    def inspect_csv(path, columns, delimiter, skip_rows):
        labels = []
        invalid = []
        label_index = columns.index("n")
        numeric = [index for index, value in enumerate(columns) if value in NUMERIC_COLUMNS]
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream, delimiter=delimiter)
            for _ in range(skip_rows):
                next(rows, None)
            for line_number, row in enumerate(rows, start=skip_rows + 1):
                if not row or all(not value.strip() for value in row):
                    continue
                if len(row) != len(columns):
                    invalid.append({"line": line_number, "error": "column_count",
                                    "actual": len(row), "expected": len(columns)})
                    continue
                label = row[label_index].strip()
                if not label:
                    invalid.append({"line": line_number, "error": "empty_label"})
                    continue
                try:
                    for index in numeric:
                        if row[index].strip():
                            value = float(row[index])
                            if not math.isfinite(value):
                                raise ValueError
                except ValueError:
                    invalid.append({"line": line_number, "error": "invalid_number"})
                    continue
                labels.append(label)
        if not labels and not invalid:
            raise ValueError("После пропущенных строк CSV не содержит данных.")
        counts = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        duplicates = sorted(label for label, count in counts.items() if count > 1)
        return labels, duplicates, invalid

    async def prepare(self, chunk_key, path, columns, delimiter, skip_rows, epsg, items, create_markers):
        path = self.validate_parameters(path, columns, delimiter, skip_rows, epsg)
        if items not in ("cameras", "markers"):
            raise ValueError("items должен быть cameras или markers.")
        if items == "cameras" and create_markers:
            raise ValueError("create_markers допустим только при items=markers.")
        labels, duplicates, invalid = self.inspect_csv(path, columns, delimiter, skip_rows)
        targets = await self.bridge.call("reference_targets", chunk_key=chunk_key)
        camera_labels = set(targets["camera_labels"])
        marker_labels = set(targets["marker_labels"])
        unique = set(labels)
        selected_labels = camera_labels if items == "cameras" else marker_labels
        unmatched = sorted(unique - selected_labels)
        stat = path.stat()
        preview_id = uuid4().hex
        preview = {
            "preview_id": preview_id, "chunk_key": chunk_key, "path": str(path),
            "sha256": file_sha256(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "format": "csv", "columns": columns, "delimiter": delimiter,
            "skip_rows": skip_rows, "epsg": epsg, "items": items,
            "create_markers": bool(create_markers),
            "row_count": len(labels) + len(invalid), "valid_rows": len(labels),
            "invalid_row_count": len(invalid), "invalid_rows": invalid[:100],
            "duplicate_count": len(duplicates), "duplicates": duplicates[:100],
            "matched_cameras": len(unique & camera_labels),
            "matched_markers": len(unique & marker_labels),
            "matched_selected_items": len(unique & selected_labels),
            "unmatched_count": len(unmatched), "unmatched_labels": unmatched[:100],
        }
        self._path(preview_id).write_text(
            json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.record("reference_import_prepared", preview_id=preview_id, chunk_key=chunk_key,
                    path=str(path), valid_rows=len(labels), invalid_rows=len(invalid))
        return preview

    async def apply(self, preview_id):
        preview = self._load(preview_id)
        path = self.validate_parameters(
            preview["path"], preview["columns"], preview["delimiter"],
            preview["skip_rows"], preview["epsg"])
        stat = path.stat()
        if (stat.st_size != preview["size"] or stat.st_mtime_ns != preview["mtime_ns"]
                or file_sha256(path) != preview["sha256"]):
            raise ValueError("Reference-файл изменился после проверки. Создайте новый preview.")
        if preview["invalid_row_count"] or preview["duplicate_count"]:
            raise ValueError("Импорт заблокирован: исправьте недопустимые или повторяющиеся строки.")
        result = await self.bridge.call("reference_import", timeout=None, **{
            key: preview[key] for key in (
                "chunk_key", "path", "columns", "delimiter", "skip_rows", "epsg",
                "items", "create_markers")
        })
        self.record("reference_import_completed", preview_id=preview_id,
                    chunk_key=preview["chunk_key"], path=preview["path"])
        return {"preview_id": preview_id, **result}
