"""Persistent Metashape project restore points."""

import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import Metashape


def data_file_state(directory):
    """mtime/size of every data file of a .psx project, keyed by path relative to .files."""
    root = Path(directory)
    state = {}
    if not root.is_dir():
        return state
    for path in root.rglob("*"):
        if path.name == "lock" or path.name.startswith("._") or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        state[path.relative_to(root).as_posix()] = [stat.st_mtime_ns, stat.st_size]
    return state


def _archive_refs(path):
    """Members and data paths referenced from doc.xml of a Metashape zip (None if unreadable)."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = set(archive.namelist())
            text = archive.read("doc.xml").decode("utf-8", "replace") if "doc.xml" in members else ""
    except (OSError, zipfile.BadZipFile, KeyError):
        return None, None
    refs = [ref for ref in re.findall(r'path="([^"]+)"', text)
            if not ref.startswith(("..", "/")) and "://" not in ref]
    return members, refs


def missing_references(target):
    """Data files a lightweight restore point refers to that will not exist after restore.

    Opening a project with a missing file shows a modal "Can't open file... retry?" dialog that
    blocks the call (probe 0.33), so the restore is refused before anything changes instead.
    Photos (paths outside .files) are not checked.
    """
    snapshot = Path(target["snapshot_directory"]) / "project.files"
    data_directory = Path(target["project_path"]).with_suffix(".files")
    moved = [(Path(entry["original_path"]), Path(entry["quarantine_path"]))
             for entry in target.get("moved_data", [])]

    def actual(path):
        for original, quarantine in moved:
            if not original.exists():
                try:
                    return quarantine / path.relative_to(original)
                except ValueError:
                    continue
        return path

    missing = []
    for relative in target.get("metadata_files", []):
        if not relative.endswith("frame.zip"):
            continue
        _members, refs = _archive_refs(snapshot / relative)
        frame_directory = (data_directory / relative).parent
        for ref in refs or []:
            path = actual(frame_directory / ref)
            if not path.exists():
                missing.append((frame_directory / ref).relative_to(data_directory).as_posix())
                continue
            if path.suffix.lower() != ".zip":
                continue
            members, inner = _archive_refs(path)
            if members is None:
                missing.append(f"{(frame_directory / ref).relative_to(data_directory).as_posix()} (повреждён)")
                continue
            for item in inner:
                if item not in members and not (path.parent / item).exists():
                    missing.append((frame_directory / ref).parent.joinpath(item)
                                   .relative_to(data_directory).as_posix())
    return sorted(set(missing))


def new_unreferenced_folders(target):
    """Data folders created after a lightweight restore point that the restored project ignores.

    A folder qualifies only if none of its files existed at the snapshot and the restored
    metadata does not refer to it: whole chunk folders of chunks absent from project.zip, and
    <chunk>/<frame>/<asset> folders absent from that frame's frame.zip.
    """
    snapshot = Path(target["snapshot_directory"]) / "project.files"
    data_directory = Path(target["project_path"]).with_suffix(".files")
    recorded = set(target.get("data_files") or {})
    if not data_directory.is_dir() or not recorded:
        return []

    def is_new(folder):
        prefix = folder.relative_to(data_directory).as_posix() + "/"
        return not any(name.startswith(prefix) for name in recorded)

    _members, chunk_refs = _archive_refs(snapshot / "project.zip")
    chunks = {ref.split("/")[0] for ref in chunk_refs or []}
    found = []
    for chunk_directory in sorted(data_directory.iterdir()):
        if not chunk_directory.is_dir() or not chunk_directory.name.isdigit():
            continue
        if chunk_directory.name not in chunks:
            if is_new(chunk_directory):
                found.append(chunk_directory)
            continue
        for frame_directory in sorted(chunk_directory.iterdir()):
            if not frame_directory.is_dir() or not frame_directory.name.isdigit():
                continue
            relative = frame_directory.relative_to(data_directory).as_posix() + "/frame.zip"
            _members, refs = _archive_refs(snapshot / relative)
            if refs is None:
                continue
            used = {ref.split("/")[0] for ref in refs}
            for asset in sorted(frame_directory.iterdir()):
                if (asset.is_dir() and not asset.name.startswith("._") and asset.name not in used
                        and is_new(asset)):
                    found.append(asset)
    return found


def public(metadata):
    """Restore-point metadata without the internal per-file state."""
    return {key: value for key, value in metadata.items() if key != "data_files"}


class RestorePoints:
    def __init__(self, directory):
        self.directory = Path(directory) / "restore-points"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, restore_id):
        return self.directory / f"{restore_id}.json"

    def _load(self, restore_id):
        path = self._metadata_path(restore_id)
        if not path.is_file():
            raise ValueError("Точка восстановления не найдена.")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_metadata(self, metadata):
        self._metadata_path(metadata["restore_point_id"]).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return public(metadata)

    def create(self, reason, save_current=True, full=False):
        document = Metashape.app.document
        if document is None or not document.path:
            restore_id = uuid4().hex
            return self._write_metadata({
                "restore_point_id": restore_id,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "reason": reason, "kind": "empty", "project_path": None,
                "project_name": None, "chunk_count": 0,
            })
        if document.read_only:
            raise ValueError("Нельзя создать точку восстановления проекта только для чтения.")
        original_path = str(Path(document.path).resolve())
        if save_current:
            document.save()
        restore_id = uuid4().hex
        metadata = {
            "restore_point_id": restore_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "reason": reason,
            "project_path": original_path,
            "project_name": Path(original_path).name,
            "chunk_count": len(document.chunks),
        }
        if Path(original_path).suffix.lower() == ".psx" and not full:
            snapshot_directory = self.directory / restore_id
            snapshot_directory.mkdir()
            shutil.copy2(original_path, snapshot_directory / "project.psx")
            data_directory = Path(original_path).with_suffix(".files")
            copied = []
            # masks.zip is rewritten in place by generateMasks(replace_asset=True), so it must be
            # part of the lightweight snapshot; other heavy assets are written as new files.
            for name in ("project.zip", "chunk.zip", "frame.zip", "masks.zip"):
                for source in data_directory.rglob(name):
                    relative = source.relative_to(data_directory)
                    target = snapshot_directory / "project.files" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(relative.as_posix())
            metadata.update({"kind": "psx_metadata", "snapshot_directory": str(snapshot_directory),
                             "metadata_files": copied,
                             "data_files": data_file_state(data_directory)})
        else:
            archive_path = self.directory / f"{restore_id}.psz"
            current_path = document.path
            document.save(str(archive_path))
            document.save(current_path)
            metadata.update({"kind": "full_archive", "archive_path": str(archive_path)})
        return self._write_metadata(metadata)

    def list(self, offset, limit):
        entries = []
        for path in self.directory.glob("*.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if (metadata.get("kind") == "empty"
                        or metadata.get("kind") == "full_archive"
                        and Path(metadata["archive_path"]).is_file()
                        or metadata.get("kind") == "psx_metadata"
                        and Path(metadata["snapshot_directory"]).is_dir()):
                    entries.append(public(metadata))
            except (OSError, KeyError, ValueError, TypeError):
                continue
        entries.sort(key=lambda item: item["created_at"], reverse=True)
        stop = min(offset + limit, len(entries))
        return {"restore_points": entries[offset:stop], "total": len(entries),
                "next_offset": stop if stop < len(entries) else None}

    def register_outputs(self, restore_id, paths):
        metadata = self._load(restore_id)
        outputs = metadata.setdefault("generated_outputs", [])
        known = {item["path"] for item in outputs}
        for value in paths:
            path = Path(value).resolve()
            if (not path.is_dir()
                    or not path.name.startswith(("metashape-obj-", "metashape-raster-",
                                                 "metashape-reference-", "metashape-model-",
                                                 "metashape-point-cloud-", "metashape-cameras-",
                                                 "metashape-report-", "metashape-contours-",
                                                 "metashape-shapes-", "metashape-renders-",
                                                 "metashape-sensor-calibration-",
                                                 "metashape-tiled-model-"))):
                raise ValueError("Можно зарегистрировать только созданный MCP каталог экспорта.")
            if str(path) not in known:
                outputs.append({"path": str(path), "kind": "directory"})
                known.add(str(path))
        self._write_metadata(metadata)
        return {"restore_point_id": restore_id, "generated_outputs": outputs}

    def record_moved_data(self, restore_id, entries):
        """Remember project data folders moved aside by an operation (e.g. a removed chunk)."""
        metadata = self._load(restore_id)
        stored = metadata.setdefault("moved_data", [])
        for entry in entries:
            stored.append({"original_path": entry["original_path"],
                           "quarantine_path": entry["quarantine_path"],
                           "bytes": entry.get("bytes"),
                           "moved_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})
        self._write_metadata(metadata)

    def return_moved_data(self, metadata):
        """Move data folders recorded by record_moved_data back; never overwrite."""
        returned, problems = [], []
        for entry in metadata.get("moved_data", []):
            source, destination = Path(entry["quarantine_path"]), Path(entry["original_path"])
            if destination.exists():
                problems.append(f"{destination}: путь уже занят")
            elif not source.exists():
                problems.append(f"{source}: данных в карантине нет")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
                returned.append(str(destination))
        return returned, problems

    def quarantine_outputs(self, metadata):
        moved = []
        for item in metadata.get("generated_outputs", []):
            source = Path(item["path"])
            if not source.exists():
                continue
            quarantine = (source.parent / ".metashape-mcp-quarantine"
                          / metadata["restore_point_id"] / source.name)
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            if quarantine.exists():
                raise ValueError(f"Каталог карантина уже существует: {quarantine}")
            shutil.move(str(source), str(quarantine))
            existing = next((entry for entry in metadata.get("quarantined_outputs", [])
                             if entry["original_path"] == str(source)
                             and entry["quarantine_path"] == str(quarantine)), None)
            entry = existing or {"quarantine_id": uuid4().hex,
                                 "original_path": str(source),
                                 "quarantine_path": str(quarantine)}
            entry["moved_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            entry.pop("restored_at", None)
            moved.append(entry)
        if moved:
            stored = metadata.setdefault("quarantined_outputs", [])
            stored.extend(entry for entry in moved if entry not in stored)
            self._write_metadata(metadata)
        return moved

    def list_quarantined(self, offset, limit):
        entries = []
        for metadata_path in self.directory.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            changed = False
            for item in metadata.get("quarantined_outputs", []):
                if not item.get("quarantine_id"):
                    item["quarantine_id"] = uuid4().hex
                    changed = True
                quarantine_exists = Path(item["quarantine_path"]).exists()
                original_exists = Path(item["original_path"]).exists()
                state = "quarantined" if quarantine_exists else "restored" if original_exists else "missing"
                entries.append({**item, "state": state,
                                "restore_point_id": metadata["restore_point_id"]})
            if changed:
                self._write_metadata(metadata)
        entries.sort(key=lambda item: item.get("moved_at", ""), reverse=True)
        stop = min(offset + limit, len(entries))
        return {"outputs": entries[offset:stop], "total": len(entries),
                "next_offset": stop if stop < len(entries) else None}

    def restore_quarantined(self, quarantine_id):
        for metadata_path in self.directory.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            for item in metadata.get("quarantined_outputs", []):
                if item.get("quarantine_id") != quarantine_id:
                    continue
                source = Path(item["quarantine_path"])
                destination = Path(item["original_path"])
                if not source.exists():
                    raise ValueError("Данные в карантине отсутствуют.")
                if destination.exists():
                    raise ValueError("Исходный путь уже занят; восстановление отменено.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                item["restored_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                self._write_metadata(metadata)
                return {"quarantine_id": quarantine_id, "state": "restored",
                        "restored_path": str(destination),
                        "restore_point_id": metadata["restore_point_id"]}
        raise ValueError("Запись карантина не найдена.")

    def restore(self, restore_id):
        target = self._load(restore_id)
        document = Metashape.app.document
        target_path = Path(target["project_path"]).resolve() if target.get("project_path") else None
        current_path = Path(document.path).resolve() if document is not None and document.path else None
        if (target_path is not None and target_path.suffix.lower() == ".psx"
                and target_path != current_path):
            lock_path = target_path.with_suffix(".files") / "lock"
            if lock_path.exists():
                raise ValueError(
                    f"Целевой проект заблокирован Metashape: {lock_path}. "
                    "Закройте другой экземпляр или подтвердите удаление устаревшего lock вручную."
                )
        if target["kind"] == "psx_metadata":
            missing_now = missing_references(target)
            if missing_now:
                raise ValueError(
                    "Откат отменён до изменений: проект в этой точке ссылается на файлы, которых "
                    f"больше нет ({len(missing_now)}): " + ", ".join(missing_now[:8])
                    + (" …" if len(missing_now) > 8 else "")
                    + ". Metashape остановилась бы на окне «Can't open file».")
        safety = self.create(f"before_restore:{restore_id}", save_current=True, full=False)
        quarantined = self.quarantine_outputs(target)
        placeholder_path = self.directory / f"restore-placeholder-{uuid4().hex}.psx"
        placeholder = Metashape.Document()
        placeholder.save(str(placeholder_path))
        del placeholder
        document.open(str(placeholder_path), read_only=False)
        if target["kind"] == "empty":
            return {"restored": target, "safety_restore_point": safety,
                    "active_project_path": document.path}
        if target["kind"] == "full_archive":
            archive_path = Path(target["archive_path"])
            if not archive_path.is_file():
                raise ValueError("Архив точки восстановления отсутствует.")
            document.open(str(archive_path), read_only=False)
            document.save(target["project_path"])
        elif target["kind"] == "psx_metadata":
            snapshot_directory = Path(target["snapshot_directory"])
            project_path = Path(target["project_path"])
            # Files that existed at the snapshot, were rewritten in place since, and are not part
            # of the lightweight snapshot cannot be rolled back. Report them instead of hiding it.
            moved_back, move_problems = self.return_moved_data(target)
            recorded = target.get("data_files") or {}
            current = data_file_state(project_path.with_suffix(".files"))
            covered = set(target["metadata_files"])
            not_restored = sorted(name for name, value in recorded.items()
                                  if name in current and tuple(current[name]) != tuple(value)
                                  and name not in covered)
            # Files deleted since the snapshot (e.g. by Metashape on save) are lost as well.
            missing = sorted(name for name in recorded
                             if name not in current and name not in covered)
            shutil.copy2(snapshot_directory / "project.psx", project_path)
            data_directory = project_path.with_suffix(".files")
            for relative in target["metadata_files"]:
                source = snapshot_directory / "project.files" / relative
                destination = data_directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            # Data built after the snapshot (new clouds, job copies) would stay on disk without
            # any reference; move it next to the project into quarantine, restorable by ID.
            orphans = []
            quarantine_root = (project_path.parent / ".metashape-mcp-quarantine"
                               / f"{restore_id}-after-restore")
            for folder in new_unreferenced_folders(target):
                destination = quarantine_root / folder.relative_to(data_directory)
                destination.parent.mkdir(parents=True, exist_ok=True)
                folder.rename(destination)
                orphans.append({"quarantine_id": uuid4().hex, "original_path": str(folder),
                                "quarantine_path": str(destination),
                                "moved_at": datetime.now(timezone.utc).isoformat(
                                    timespec="milliseconds")})
            if orphans:
                target.setdefault("quarantined_outputs", []).extend(orphans)
                self._write_metadata(target)
        else:
            raise ValueError("Неизвестный формат точки восстановления.")
        document.open(target["project_path"], read_only=False)
        result = {"restored": target, "safety_restore_point": safety,
                  "active_project_path": document.path, "quarantined_outputs": quarantined}
        if target["kind"] == "psx_metadata":
            result["changed_files_not_restored"] = not_restored
            result["missing_files_not_restored"] = missing
            result["new_data_quarantined"] = [entry["original_path"] for entry in orphans]
            result["moved_data_returned"] = moved_back
            if move_problems:
                result["moved_data_problems"] = move_problems
            if missing:
                print("Metashape MCP: откат не нашёл удалённые с тех пор файлы: "
                      f"{missing[:10]}")
            if not_restored:
                print("Metashape MCP: откат не восстановил изменённые на месте файлы: "
                      f"{not_restored[:10]}")
        result["restored"] = {key: value for key, value in target.items() if key != "data_files"}
        return result
