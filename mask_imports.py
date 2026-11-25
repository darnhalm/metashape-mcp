"""Persistent, fingerprinted previews for importing camera masks from files.

Metashape 2.2 imports mask files through ``Chunk.generateMasks`` with
``MaskingModeFile``; there is no ``Chunk.importMasks``. The preview resolves the
template for every selected camera on the server side, so the user sees exactly
which files will be used before the project changes.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
from uuid import uuid4


PLACEHOLDER = "{filename}"
MASK_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"})
OPERATIONS = ("replacement", "union", "intersection", "difference")
LIST_LIMIT = 100


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_template(template):
    """Return the template's directory. Only the file name may contain {filename}."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("Укажите шаблон пути к файлам масок.")
    path = Path(template).expanduser()
    if not path.is_absolute():
        raise ValueError("Шаблон масок должен быть абсолютным путём, например /папка/{filename}_mask.png.")
    directory, name = path.parent, path.name
    if "{" in str(directory) or "}" in str(directory):
        raise ValueError("Подстановка {filename} допустима только в имени файла, не в папке.")
    if PLACEHOLDER not in name:
        raise ValueError("Имя файла в шаблоне должно содержать {filename}.")
    if name.replace(PLACEHOLDER, "").count("{") or name.replace(PLACEHOLDER, "").count("}"):
        raise ValueError("Поддерживается только подстановка {filename}.")
    if PurePosixPath(name.replace(PLACEHOLDER, "x")).suffix.lower() not in MASK_SUFFIXES:
        raise ValueError("Файл маски должен быть PNG, JPEG, TIFF или BMP.")
    return directory


def resolve_mask_path(template, photo_path):
    stem = Path(photo_path).stem
    return Path(template.replace(PLACEHOLDER, stem)).expanduser()


def fingerprint(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": file_sha256(path)}


class MaskImports:
    def __init__(self, directory, bridge, record):
        self.directory = Path(directory) / "mask-previews"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.record = record

    def _path(self, preview_id):
        if not isinstance(preview_id, str) or len(preview_id) != 32:
            raise ValueError("Неверный ID предварительной проверки масок.")
        return self.directory / f"{preview_id}.json"

    def load(self, preview_id):
        path = self._path(preview_id)
        if not path.is_file():
            raise ValueError("Предварительная проверка масок не найдена.")
        return json.loads(path.read_text(encoding="utf-8"))

    async def _targets(self, chunk_key, camera_keys):
        return await self.bridge.call(
            "mask_import_targets", timeout=None, chunk_key=chunk_key, camera_keys=camera_keys)

    async def prepare(self, chunk_key, template, camera_keys=None, operation="replacement"):
        directory = validate_template(template)
        if operation not in OPERATIONS:
            raise ValueError(f"operation должен быть одним из: {', '.join(OPERATIONS)}.")
        if camera_keys is not None:
            if not camera_keys or len(set(camera_keys)) != len(camera_keys):
                raise ValueError("camera_keys должен быть непустым списком без повторов.")
        if not directory.is_dir():
            raise ValueError(f"Папка масок не найдена: {directory}")
        targets = await self._targets(chunk_key, camera_keys)
        found, missing = [], []
        for camera in targets["cameras"]:
            if not camera.get("photo_path"):
                missing.append({"key": camera["key"], "label": camera["label"], "reason": "no_photo"})
                continue
            path = resolve_mask_path(template, camera["photo_path"])
            if path.is_file():
                found.append({"key": camera["key"], "label": camera["label"],
                              "photo_path": camera["photo_path"], "mask_path": str(path),
                              "has_mask": camera["has_mask"], **fingerprint(path)})
            else:
                missing.append({"key": camera["key"], "label": camera["label"],
                                "mask_path": str(path), "reason": "file_not_found"})
        preview_id = uuid4().hex
        preview = {
            "preview_id": preview_id, "chunk_key": chunk_key, "template": template,
            "directory": str(directory), "operation": operation,
            "camera_keys": camera_keys, "requested_cameras": len(targets["cameras"]),
            "found_count": len(found), "missing_count": len(missing),
            "replaced_existing_masks": sum(item["has_mask"] for item in found),
            "found": found, "missing": missing,
        }
        self._path(preview_id).write_text(
            json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.record("mask_import_prepared", preview_id=preview_id, chunk_key=chunk_key,
                    template=template, found=len(found), missing=len(missing), operation=operation)
        return self.public(preview)

    @staticmethod
    def public(preview):
        """Return the preview with long per-camera lists capped."""
        result = {key: value for key, value in preview.items() if key not in ("found", "missing")}
        result["found"] = [{key: item[key] for key in ("key", "label", "mask_path", "has_mask")}
                           for item in preview["found"][:LIST_LIMIT]]
        result["missing"] = preview["missing"][:LIST_LIMIT]
        result["lists_truncated"] = max(len(preview["found"]), len(preview["missing"])) > LIST_LIMIT
        return result

    async def apply(self, preview_id, job_id=None):
        preview = self.load(preview_id)
        if not preview["found"]:
            raise ValueError("Импорт невозможен: ни для одной камеры не найден файл маски.")
        current = await self._targets(preview["chunk_key"], [item["key"] for item in preview["found"]])
        photos = {camera["key"]: camera.get("photo_path") for camera in current["cameras"]}
        for item in preview["found"]:
            if photos.get(item["key"]) != item["photo_path"]:
                raise ValueError(f"Камера {item['label']} изменилась после проверки. Создайте новый preview.")
            path = Path(item["mask_path"])
            if not path.is_file() or fingerprint(path) != {
                    key: item[key] for key in ("size", "mtime_ns", "sha256")}:
                raise ValueError(f"Файл маски изменился после проверки: {path}. Создайте новый preview.")
        result = await self.bridge.call(
            "mask_import", timeout=None, job_id=job_id, chunk_key=preview["chunk_key"], template=preview["template"],
            camera_keys=[item["key"] for item in preview["found"]], operation=preview["operation"])
        self.record("mask_import_completed", preview_id=preview_id, chunk_key=preview["chunk_key"],
                    imported=result.get("cameras_with_mask_after"))
        return {"preview_id": preview_id, "missing_count": preview["missing_count"], **result}


GENERATION_MODES = ("alpha", "background", "model")
JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_has_alpha(path):
    """Read the PNG header: True for an alpha channel or transparency chunk, False otherwise."""
    with Path(path).open("rb") as stream:
        header = stream.read(33)
        if len(header) < 26 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
            return None
        if header[25] in (4, 6):  # grey + alpha, RGBA
            return True
        stream.seek(8)
        while True:  # look for tRNS before the image data
            chunk = stream.read(8)
            if len(chunk) < 8:
                return False
            length, kind = int.from_bytes(chunk[:4], "big"), chunk[4:]
            if kind == b"tRNS":
                return True
            if kind in (b"IDAT", b"IEND"):
                return False
            stream.seek(length + 4, 1)


def alpha_support(photo_path):
    """Return (usable, reason) for alpha masking of one photo file."""
    path = Path(photo_path)
    suffix = path.suffix.lower()
    if suffix in JPEG_SUFFIXES:
        return False, "jpeg_has_no_alpha"
    if suffix == ".png":
        alpha = png_has_alpha(path)
        return (True, "alpha") if alpha else (False, "no_alpha_channel")
    return True, "alpha_unknown"


def quick_fingerprint(path):
    stat = Path(path).stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


class MaskGeneration(MaskImports):
    """Preview and run alpha/background mask generation (Chunk.generateMasks)."""

    def __init__(self, directory, bridge, record):
        self.directory = Path(directory) / "mask-generation-previews"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.record = record

    async def prepare(self, chunk_key, mode, camera_keys=None, operation="replacement",
                      tolerance=10, background_template=None):
        if mode not in GENERATION_MODES:
            raise ValueError("mode должен быть alpha, background или model.")
        if operation not in OPERATIONS:
            raise ValueError(f"operation должен быть одним из: {', '.join(OPERATIONS)}.")
        if not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
            raise ValueError("tolerance должен быть целым числом от 0 до 255.")
        if camera_keys is not None and (not camera_keys or len(set(camera_keys)) != len(camera_keys)):
            raise ValueError("camera_keys должен быть непустым списком без повторов.")
        directory = None
        if mode == "background":
            if background_template is None:
                raise ValueError("Для mode=background укажите background_template.")
            directory = validate_template(background_template)
            if not directory.is_dir():
                raise ValueError(f"Папка снимков фона не найдена: {directory}")
        elif background_template is not None:
            raise ValueError("background_template используется только с mode=background.")
        targets = await self._targets(chunk_key, camera_keys)
        if mode == "model" and not targets.get("has_model"):
            raise ValueError("Для масок по модели в чанке нужна активная модель.")
        found, missing = [], []
        for camera in targets["cameras"]:
            base = {"key": camera["key"], "label": camera["label"]}
            photo = camera.get("photo_path")
            if not photo or not Path(photo).is_file():
                missing.append({**base, "reason": "photo_not_found"})
                continue
            item = {**base, "photo_path": photo, "has_mask": camera["has_mask"],
                    "photo": quick_fingerprint(photo)}
            if mode == "model":
                if not camera.get("aligned"):
                    missing.append({**base, "reason": "camera_not_aligned"})
                    continue
            elif mode == "alpha":
                usable, reason = alpha_support(photo)
                if not usable:
                    missing.append({**base, "reason": reason})
                    continue
                item["alpha"] = reason
            else:
                background = resolve_mask_path(background_template, photo)
                if not background.is_file():
                    missing.append({**base, "background_path": str(background),
                                    "reason": "background_not_found"})
                    continue
                item["background_path"] = str(background)
                item["background"] = quick_fingerprint(background)
            found.append(item)
        preview_id = uuid4().hex
        preview = {
            "preview_id": preview_id, "chunk_key": chunk_key, "mode": mode,
            "operation": operation, "tolerance": tolerance,
            "template": background_template, "directory": str(directory) if directory else None,
            "camera_keys": camera_keys, "requested_cameras": len(targets["cameras"]),
            "found_count": len(found), "missing_count": len(missing),
            "replaced_existing_masks": sum(item["has_mask"] for item in found),
            "alpha_unknown": sum(item.get("alpha") == "alpha_unknown" for item in found),
            "found": found, "missing": missing,
        }
        self._path(preview_id).write_text(
            json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.record("mask_generation_prepared", preview_id=preview_id, chunk_key=chunk_key,
                    mode=mode, found=len(found), missing=len(missing), operation=operation)
        return self.public(preview)

    @staticmethod
    def public(preview):
        result = {key: value for key, value in preview.items() if key not in ("found", "missing")}
        result["found"] = [
            {key: item[key] for key in ("key", "label", "has_mask", "alpha", "background_path")
             if key in item} for item in preview["found"][:LIST_LIMIT]]
        result["missing"] = preview["missing"][:LIST_LIMIT]
        result["lists_truncated"] = max(len(preview["found"]), len(preview["missing"])) > LIST_LIMIT
        return result

    async def apply(self, preview_id, job_id=None):
        preview = self.load(preview_id)
        if not preview["found"]:
            raise ValueError("Создание масок невозможно: нет подходящих камер.")
        current = await self._targets(preview["chunk_key"], [item["key"] for item in preview["found"]])
        photos = {camera["key"]: camera.get("photo_path") for camera in current["cameras"]}
        for item in preview["found"]:
            if photos.get(item["key"]) != item["photo_path"]:
                raise ValueError(f"Камера {item['label']} изменилась после проверки. Создайте новый preview.")
            checks = [(item["photo_path"], item["photo"])]
            if "background_path" in item:
                checks.append((item["background_path"], item["background"]))
            for path, expected in checks:
                if not Path(path).is_file() or quick_fingerprint(path) != expected:
                    raise ValueError(f"Файл изменился после проверки: {path}. Создайте новый preview.")
        result = await self.bridge.call(
            "mask_generate", timeout=None, job_id=job_id, chunk_key=preview["chunk_key"], mode=preview["mode"],
            template=preview["template"], camera_keys=[item["key"] for item in preview["found"]],
            operation=preview["operation"], tolerance=preview["tolerance"])
        self.record("mask_generation_completed", preview_id=preview_id, chunk_key=preview["chunk_key"],
                    mode=preview["mode"], masked=result.get("cameras_with_mask_after"))
        return {"preview_id": preview_id, "missing_count": preview["missing_count"], **result}
