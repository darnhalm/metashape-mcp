# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///
"""Local MCP server. Its private stdin/stdout connect only to the Metashape launcher."""

import asyncio
import base64
import functools
import inspect
import hmac
import json
import math
import os
import secrets
import shutil
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import uvicorn
from mcp import MCPError, types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from processing_jobs import INPLACE_KINDS, ProcessingJobs
from progress_tracker import stage_progress
from reference_data import ReferenceImports
from mask_imports import MaskGeneration, MaskImports
from point_cloud_imports import PointCloudImports
from model_roundtrip import inspect_reimport
from shape_imports import ShapeImports
from path_policy import resolve_scoped
from workflow_catalog import (
    aerial_workflow_prompt,
    alignment_diagnostic_prompt,
    batch_export_prompt,
    gis_quarry_workflow_prompt,
    object_workflow_prompt,
    quality_settings_prompt,
    safe_workflow_catalog,
)


# Failures raised on purpose by our checks or reported by Metashape through the bridge.
# Their text is shown to the client; any other exception stays a hidden crash.
EXPECTED_ERRORS = (ValueError, RuntimeError, OSError, LookupError)


class HostBridge:
    def __init__(self, event_handler):
        self.pending = {}
        self.closed = False
        self.event_handler = event_handler

    async def call(self, method, *, timeout=5, **parameters):
        if self.closed:
            raise RuntimeError("Соединение с Metashape закрыто.")
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            print(json.dumps({"id": key, "method": method, **parameters}), flush=True)
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as error:
            raise RuntimeError(f"Metashape не ответила за {timeout} секунд. Возможно, приложение занято.") from error
        finally:
            self.pending.pop(key, None)

    def receive(self, line):
        reply = json.loads(line)
        if "id" not in reply:
            self.event_handler(reply)
            return
        future = self.pending.get(reply["id"])
        if future is not None and not future.done():
            if "error" in reply:
                future.set_exception(RuntimeError(reply["error"]))
            else:
                future.set_result(reply["result"])

    def disconnect(self, web_server):
        self.closed = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Соединение с Metashape закрыто."))
        web_server.should_exit = True

    def listen(self, loop, web_server):
        try:
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(self.receive, line)
        finally:
            loop.call_soon_threadsafe(self.disconnect, web_server)


async def serve(port, directory):
    # Reserve the endpoint before touching the journal or creating credentials.
    with socket.create_server(("127.0.0.1", port)) as listener:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        token_file = directory / "access-token"
        try:
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
        token = token_file.read_text().strip()
        if not token:
            raise RuntimeError("Файл токена пуст.")
        journal = directory / "service.jsonl"
        run = uuid4().hex

        def record(event, **fields):
            entry = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "run_id": run, "event": event, **fields}
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

        async def authorize(ctx, call_next):
            public = {"server/discover", "initialize", "notifications/initialized", "tools/list", "ping"}
            if ctx.method not in public:
                headers = ctx.request.headers if ctx.request is not None else {}
                value = headers.get("authorization", "")
                if not hmac.compare_digest(value.encode(), ("Bearer " + token).encode()):
                    raise MCPError(-32001, "Требуется локальный токен Metashape MCP.")
            return await call_next(ctx)

        def host_event(message):
            if message.get("event") != "metashape_console":
                record("host_event_ignored", message_type=message.get("event"))
                return
            common = {"source": "Metashape.app.console_pane.contents"}
            if message.get("job_id"):
                common["job_id"] = message["job_id"]
            if message.get("operation"):
                common["operation"] = message["operation"]
            for item in message.get("lines", []):
                record("metashape_console", line=item["text"], truncated=item["truncated"], **common)

        bridge = HostBridge(host_event)
        jobs = ProcessingJobs(directory, bridge, record)
        reference_imports = ReferenceImports(directory, bridge, record)
        mask_imports = MaskImports(directory, bridge, record)
        mask_generation = MaskGeneration(directory, bridge, record)
        point_cloud_imports = PointCloudImports(directory, bridge, record)
        shape_imports = ShapeImports(directory, bridge, record)
        mcp = MCPServer("metashape-mcp", title="Metashape MCP", version="0.46.0",
                        middleware=[authorize])
        read_only = types.ToolAnnotations(read_only_hint=True, destructive_hint=False)
        register_tool = mcp.tool

        def tool(*args, **kwargs):
            """Register a tool whose anticipated failures reach the client with their message."""
            decorator = register_tool(*args, **kwargs)

            def apply(function):
                if inspect.iscoroutinefunction(function):
                    @functools.wraps(function)
                    async def wrapped(*call_args, **call_kwargs):
                        try:
                            return await function(*call_args, **call_kwargs)
                        except EXPECTED_ERRORS as error:
                            raise ToolError(str(error) or type(error).__name__) from error
                else:
                    @functools.wraps(function)
                    def wrapped(*call_args, **call_kwargs):
                        try:
                            return function(*call_args, **call_kwargs)
                        except EXPECTED_ERRORS as error:
                            raise ToolError(str(error) or type(error).__name__) from error
                return decorator(wrapped)
            return apply

        mcp.tool = tool

        async def host_call(method, *, timeout=5, **parameters):
            request_id = uuid4().hex
            record("requested", tool=method, request_id=request_id)
            if method == "user_script_run":
                record("user_script_requested", request_id=request_id, path=parameters["path"])
            try:
                result = await bridge.call(method, timeout=timeout, **parameters)
            except Exception as error:
                record("failed", tool=method, request_id=request_id, message=str(error))
                raise
            if isinstance(result, dict) and result.get("restore_point"):
                point = result["restore_point"]
                record("restore_point_created", tool=method, request_id=request_id,
                       restore_point_id=point["restore_point_id"], reason=point["reason"],
                       kind=point["kind"])
            if method == "restore_point_create":
                record("restore_point_created", tool=method, request_id=request_id,
                       restore_point_id=result["restore_point_id"], reason=result["reason"],
                       kind=result["kind"])
            if method == "restore_point_restore":
                record("restore_point_restored", request_id=request_id,
                       restore_point_id=result["restored"]["restore_point_id"],
                       safety_restore_point_id=result["safety_restore_point"]["restore_point_id"])
            if method == "user_script_run":
                record("user_script_completed", request_id=request_id, path=result["path"],
                       sha256=result["sha256"], external_changes_reversible=False)
            record("completed", tool=method, request_id=request_id)
            return result

        async def validate_paths(*paths):
            return await host_call("paths_validate", timeout=None, paths=list(paths))

        def resource_json(value):
            return json.dumps(value, ensure_ascii=False, indent=2)

        @mcp.resource(
            "metashape://service/status",
            name="metashape_service_status",
            title="Состояние Metashape MCP",
            description="Версия Metashape и идентификатор текущего запуска MCP.",
            mime_type="application/json",
        )
        async def service_status_resource() -> str:
            return resource_json({**await host_call("get_service_status"), "server_run_id": run})

        @mcp.resource(
            "metashape://project/summary",
            name="metashape_project_summary",
            title="Сводка открытого проекта",
            description="Только чтение: путь, состояние документа и активный чанк.",
            mime_type="application/json",
        )
        async def project_summary_resource() -> str:
            return resource_json(await host_call("get_project_summary"))

        @mcp.resource(
            "metashape://project/chunks",
            name="metashape_project_chunks",
            title="Чанки открытого проекта",
            description="Первые 100 чанков с ключами и числом камер; для продолжения используйте list_chunks.",
            mime_type="application/json",
        )
        async def project_chunks_resource() -> str:
            return resource_json(await host_call("list_chunks", offset=0, limit=100))

        @mcp.resource(
            "metashape://capabilities/safe-workflows",
            name="metashape_safe_workflows",
            title="Безопасные сценарии Metashape MCP",
            description="Контракт prepare/start/wait/accept-or-reject, экспортов и пользовательских скриптов.",
            mime_type="application/json",
        )
        def safe_workflows_resource() -> str:
            return resource_json(safe_workflow_catalog())

        @mcp.resource(
            "metashape://viewport/current",
            name="metashape_current_viewport",
            title="Текущий viewport Metashape",
            description="PNG 1280×720 текущего вида модели без сохранения файла или изменения проекта.",
            mime_type="image/png",
        )
        async def current_viewport_resource() -> bytes:
            result = await host_call("capture_viewport", timeout=30, width=1280, height=720)
            return base64.b64decode(result["base64"], validate=True)

        @mcp.resource(
            "metashape://chunk/{chunk_key}/summary",
            name="metashape_chunk_summary",
            title="Сводка чанка",
            description="Камеры, выравнивание, опорные данные и активы точного ключа чанка.",
            mime_type="application/json",
        )
        async def chunk_summary_resource(chunk_key: int) -> str:
            alignment = await host_call("get_alignment_statistics", timeout=None, chunk_key=chunk_key)
            references = await host_call("get_reference_summary", chunk_key=chunk_key)
            assets = await host_call("list_chunk_assets", chunk_key=chunk_key)
            return resource_json({"chunk_key": chunk_key, "alignment": alignment,
                                  "references": references, "assets": assets})

        @mcp.resource(
            "metashape://chunk/{chunk_key}/cameras",
            name="metashape_chunk_cameras",
            title="Камеры чанка",
            description="Первые 100 камер; продолжение доступно через list_cameras.",
            mime_type="application/json",
        )
        async def chunk_cameras_resource(chunk_key: int) -> str:
            return resource_json(await host_call("list_cameras", chunk_key=chunk_key,
                                                 offset=0, limit=100))

        @mcp.resource(
            "metashape://chunk/{chunk_key}/sensors",
            name="metashape_chunk_sensors",
            title="Сенсоры чанка",
            description="Первые 100 сенсоров и их калибровки.",
            mime_type="application/json",
        )
        async def chunk_sensors_resource(chunk_key: int) -> str:
            return resource_json(await host_call("list_sensors", chunk_key=chunk_key,
                                                 offset=0, limit=100))

        @mcp.resource(
            "metashape://chunk/{chunk_key}/tie-points",
            name="metashape_chunk_tie_points",
            title="Связующие точки чанка",
            description="Количество точек/проекций и ошибки репроекции.",
            mime_type="application/json",
        )
        async def chunk_tie_points_resource(chunk_key: int) -> str:
            return resource_json(await host_call("get_alignment_statistics", timeout=None,
                                                 chunk_key=chunk_key))

        @mcp.resource(
            "metashape://chunk/{chunk_key}/point-cloud",
            name="metashape_chunk_point_cloud",
            title="Облака точек чанка",
            description="Счётчики, классы, границы и CRS всех облаков точек.",
            mime_type="application/json",
        )
        async def chunk_point_cloud_resource(chunk_key: int) -> str:
            return resource_json(await host_call("get_point_cloud_statistics", timeout=None,
                                                 chunk_key=chunk_key, point_cloud_key=None))

        @mcp.resource(
            "metashape://chunk/{chunk_key}/model",
            name="metashape_chunk_model",
            title="Модели чанка",
            description="Геометрия, топология, площадь и текстуры всех моделей.",
            mime_type="application/json",
        )
        async def chunk_model_resource(chunk_key: int) -> str:
            return resource_json(await host_call("get_model_statistics", timeout=None,
                                                 chunk_key=chunk_key, model_key=None))

        @mcp.resource(
            "metashape://chunk/{chunk_key}/dem",
            name="metashape_chunk_dem",
            title="Активная DEM чанка",
            description="Размер, разрешение, границы, высоты, площадь покрытия и CRS активной DEM.",
            mime_type="application/json",
        )
        async def chunk_dem_resource(chunk_key: int) -> str:
            return resource_json(await host_call("get_elevation_statistics", timeout=None,
                                                 chunk_key=chunk_key, elevation_key=None))

        @mcp.resource(
            "metashape://chunk/{chunk_key}/orthomosaic",
            name="metashape_chunk_orthomosaic",
            title="Ортомозаики чанка",
            description="Размеры, разрешение, границы и CRS ортомозаик.",
            mime_type="application/json",
        )
        async def chunk_orthomosaic_resource(chunk_key: int) -> str:
            return resource_json(await host_call("get_orthomosaic_statistics", timeout=None,
                                                 chunk_key=chunk_key))

        @mcp.prompt(
            name="metashape_object_workflow",
            title="Предметная съёмка",
            description="Консервативный план получения и проверки текстурированной предметной модели.",
        )
        def object_workflow(goal: str = "текстурированная 3D-модель", notes: str = "") -> str:
            return object_workflow_prompt(goal, notes)

        @mcp.prompt(
            name="metashape_aerial_workflow",
            title="Аэросъёмка",
            description="План фотограмметрии с CRS, GCP/check points, ЦМР и ортомозаикой.",
        )
        def aerial_workflow(goal: str = "ЦМР и ортомозаика", notes: str = "") -> str:
            return aerial_workflow_prompt(goal, notes)

        @mcp.prompt(
            name="metashape_gis_quarry_workflow",
            title="ГИС / карьер",
            description="План измерений, растра, контуров и тайлового экспорта с явными единицами.",
        )
        def gis_quarry_workflow(goal: str = "измерения и экспорт", notes: str = "") -> str:
            return gis_quarry_workflow_prompt(goal, notes)

        @mcp.prompt(
            name="metashape_diagnose_alignment",
            title="Диагностика выравнивания",
            description="Поиск невыровненных камер, относительных разрывов, ошибок и безопасного исправления.",
        )
        def diagnose_alignment(goal: str = "найти причину плохого выравнивания",
                               notes: str = "") -> str:
            return alignment_diagnostic_prompt(goal, notes)

        @mcp.prompt(
            name="metashape_batch_export",
            title="Пакетный экспорт",
            description="План проверяемого экспорта существующих продуктов без их неявного построения.",
        )
        def batch_export(goal: str = "экспортировать готовые результаты", notes: str = "") -> str:
            return batch_export_prompt(goal, notes)

        @mcp.prompt(
            name="metashape_quality_settings",
            title="Настройки качества",
            description="Черновой, сбалансированный и детальный профили на основе фактов проекта.",
        )
        def quality_settings(goal: str = "подобрать качество и оценить ресурсы",
                             notes: str = "") -> str:
            return quality_settings_prompt(goal, notes)

        @mcp.tool(annotations=read_only)
        async def get_service_status() -> dict[str, object]:
            """Read the live Metashape application version and the current server run ID."""
            return {**await host_call("get_service_status"), "server_run_id": run}

        @mcp.tool(annotations=read_only)
        async def get_project_summary() -> dict[str, object]:
            """Read the current project, number of chunks and active chunk camera/marker counts."""
            return await host_call("get_project_summary")

        @mcp.tool(annotations=read_only)
        async def preflight() -> dict[str, object]:
            """Check Metashape, project, writable service storage, free disk and active job before processing."""
            service = await host_call("get_service_status")
            project = await host_call("get_project_summary")
            points = await host_call("restore_points_list", offset=0, limit=1)
            path_policy = await host_call("path_policy")
            problems = []
            warnings = []
            if service["metashape_version"] != "2.2.0":
                warnings.append("Эта версия Metashape не проходила приёмочные испытания MCP.")
            if not project["path"]:
                warnings.append("Проект ещё не сохранён: точки восстановления будут ограничены.")
            if project["read_only"]:
                problems.append("Проект открыт только для чтения.")
            if not path_policy["enabled"]:
                problems.append("В интерфейсе не выбраны разрешённые рабочие папки.")
            elif not path_policy["all_roots_available"]:
                problems.append("Одна или несколько разрешённых рабочих папок недоступны.")
            elif project["path"]:
                try:
                    resolve_scoped(project["path"], [item["path"] for item in path_policy["roots"]])
                except ValueError as error:
                    problems.append(str(error))
            try:
                usage = shutil.disk_usage(directory)
                free_bytes = usage.free
            except OSError as error:
                free_bytes = None
                problems.append(f"Не удалось проверить свободное место: {error}")
            if not os.access(directory, os.W_OK):
                problems.append("Каталог сервиса недоступен для записи.")
            return {
                "ok": not problems, "problems": problems, "warnings": warnings,
                "service": service, "project": project, "active_job": jobs.active_job(),
                "restore_point_count": points["total"], "service_directory": str(directory),
                "free_bytes": free_bytes, "path_policy": path_policy,
            }

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def create_project(path: str) -> dict[str, object]:
            """Create and open a new .psx or .psz project. Refuses existing targets and unsaved current changes."""
            jobs.assert_idle()
            await validate_paths(path)
            return await host_call("create_project", timeout=None, path=path)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def open_project(path: str) -> dict[str, object]:
            """Open an existing .psx or .psz project. Refuses to discard unsaved current changes."""
            jobs.assert_idle()
            await validate_paths(path)
            return await host_call("open_project", timeout=None, path=path)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def save_project() -> dict[str, object]:
            """Save the current writable project to its existing path."""
            jobs.assert_idle()
            return await host_call("save_project", timeout=None)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def save_project_as(path: str) -> dict[str, object]:
            """Save the current project under a new .psx or .psz path and make it the active project."""
            jobs.assert_idle()
            await validate_paths(path)
            return await host_call("save_project_as", timeout=None, path=path)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def add_chunk(label: Annotated[str, Field(min_length=1, max_length=255)] = "Chunk") -> dict[str, object]:
            """Add a named chunk to the current writable project and make it active."""
            jobs.assert_idle()
            return await host_call("add_chunk", label=label)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def add_photos(chunk_key: int,
                             paths: Annotated[list[str], Field(min_length=1, max_length=10000)]) -> dict[str, object]:
            """Add existing photos by absolute path to a writable chunk. Rejects missing and duplicate paths."""
            jobs.assert_idle()
            await validate_paths(*paths)
            return await host_call("add_photos", timeout=None, chunk_key=chunk_key, paths=paths)

        @mcp.tool(annotations=read_only)
        async def list_chunks(offset: Annotated[int, Field(ge=0)] = 0,
                              limit: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, object]:
            """List project chunks with keys, names and camera counts. Continue with next_offset until null."""
            return await host_call("list_chunks", offset=offset, limit=limit)

        @mcp.tool(annotations=read_only)
        async def list_cameras(chunk_key: int, offset: Annotated[int, Field(ge=0)] = 0,
                               limit: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, object]:
            """List cameras in the chunk's current frame, including disabled cameras. Regular cameras report alignment."""
            return await host_call("list_cameras", chunk_key=chunk_key, offset=offset, limit=limit)

        @mcp.tool(annotations=read_only)
        async def list_chunk_assets(chunk_key: int) -> dict[str, object]:
            """List depth maps, point clouds, models and raster products, and report tie point availability for this frame."""
            return await host_call("list_chunk_assets", chunk_key=chunk_key)

        @mcp.tool(annotations=read_only)
        async def get_point_cloud_statistics(
                chunk_key: int, point_cloud_key: int | None = None) -> dict[str, object]:
            """Read point counts, classes, native extents and CRS for all or one exact point-cloud key."""
            return await host_call("get_point_cloud_statistics", timeout=None,
                                   chunk_key=chunk_key, point_cloud_key=point_cloud_key)

        @mcp.tool(annotations=read_only)
        async def get_elevation_statistics(
                chunk_key: int, elevation_key: int | None = None) -> dict[str, object]:
            """Read one exact or active DEM: dimensions, resolution, extent, min/max height, coverage and CRS."""
            return await host_call("get_elevation_statistics", timeout=None,
                                   chunk_key=chunk_key, elevation_key=elevation_key)

        @mcp.tool(annotations=read_only)
        async def compare_elevations(
                chunk_a_key: int, chunk_b_key: int,
                elevation_a_key: int | None = None, elevation_b_key: int | None = None,
                grid_size: Annotated[int, Field(ge=5, le=100)] = 25) -> dict[str, object]:
            """Read-only DEM comparison on a regular grid in their common extent. Requires identical CRS (authoritative CRS across chunks), returns B-A statistics and never estimates volume."""
            return await host_call("compare_elevations", timeout=None,
                                   chunk_a_key=chunk_a_key, chunk_b_key=chunk_b_key,
                                   elevation_a_key=elevation_a_key,
                                   elevation_b_key=elevation_b_key, grid_size=grid_size)

        @mcp.tool(annotations=read_only)
        async def get_shape_measurements(
                chunk_key: int,
                shape_keys: Annotated[list[int], Field(min_length=1, max_length=100)] | None = None,
                volume_level: Literal["none", "bestfit", "mean"] = "bestfit",
                offset: Annotated[int, Field(ge=0)] = 0,
                limit: Annotated[int, Field(ge=1, le=100)] = 40
                ) -> dict[str, object]:
            """Read measurements of existing shapes without changing them. Returns geometry type, vertices, 3D/on-DEM perimeter, fitted/on-DEM area and optional DEM volume, with explicit CRS, scale and units. Unscaled chunks are labelled in conditional chunk units, never metres."""
            if shape_keys is not None and len(set(shape_keys)) != len(shape_keys):
                raise ValueError("shape_keys содержит повторы.")
            return await host_call("get_shape_measurements", timeout=None,
                                   chunk_key=chunk_key, shape_keys=shape_keys,
                                   volume_level=volume_level, offset=offset, limit=limit)

        @mcp.tool(annotations=read_only)
        async def get_mask_summary(
                chunk_key: int, offset: Annotated[int, Field(ge=0)] = 0,
                limit: Annotated[int, Field(ge=1, le=100)] = 40,
                include_coverage: bool = False) -> dict[str, object]:
            """Read-only: count regular cameras with and without masks and list them page by page. With include_coverage=true (limit <= 20) also read each mask's size and share of masked (zero) pixels."""
            return await host_call("get_mask_summary", timeout=None, chunk_key=chunk_key, offset=offset,
                                   limit=limit, include_coverage=include_coverage)

        @mcp.tool(annotations=read_only)
        async def prepare_mask_import(
                chunk_key: int, path_template: str, camera_keys: list[int] | None = None,
                operation: Literal["replacement", "union", "intersection", "difference"] = "replacement",
        ) -> dict[str, object]:
            """Preview importing mask files without changing Metashape. path_template is absolute, e.g. /dir/{filename}_mask.png; {filename} is the photo name without extension. Reports found/missing files per camera and masks that would be replaced, and stores file fingerprints."""
            await validate_paths(str(Path(path_template).expanduser().parent))
            return await mask_imports.prepare(chunk_key, path_template, camera_keys, operation)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def import_masks(preview_id: str, background: bool = False) -> dict[str, object]:
            """Import masks for the cameras found in a previous preview, if files and cameras are unchanged. Creates a restore point first; save the project manually afterwards. background=true runs it as a job (poll wait_job for operation.percent)."""
            jobs.assert_idle()
            preview = mask_imports.load(preview_id)
            await validate_paths(preview["directory"])
            if background:
                return background_job("mask_import", preview.get("chunk_key"),
                                      lambda job_id: mask_imports.apply(preview_id, job_id=job_id))
            return await mask_imports.apply(preview_id)

        @mcp.tool(annotations=read_only)
        async def prepare_mask_generation(
                chunk_key: int, mode: Literal["alpha", "background", "model"],
                camera_keys: list[int] | None = None,
                operation: Literal["replacement", "union", "intersection", "difference"] = "replacement",
                tolerance: Annotated[int, Field(ge=0, le=255)] = 10,
                background_template: str | None = None) -> dict[str, object]:
            """Preview generating masks without changing Metashape. mode=alpha uses the photos' alpha channel (JPEG photos are excluded, PNG headers are checked, TIFF is reported as alpha_unknown). mode=background compares each photo with a background shot from background_template, an absolute path such as /dir/{filename}_background.jpg; tolerance sets how close a pixel must be to the background to be masked."""
            if background_template is not None:
                await validate_paths(str(Path(background_template).expanduser().parent))
            return await mask_generation.prepare(
                chunk_key, mode, camera_keys, operation, tolerance, background_template)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def generate_masks(preview_id: str, background: bool = False) -> dict[str, object]:
            """Generate masks for the cameras accepted by a previous preview if photos and background files are unchanged. Creates a restore point first; save the project manually afterwards. background=true runs it as a job (poll wait_job for operation.percent)."""
            jobs.assert_idle()
            preview = mask_generation.load(preview_id)
            if preview["directory"]:
                await validate_paths(preview["directory"])
            if background:
                return background_job("mask_generate", preview.get("chunk_key"),
                                      lambda job_id: mask_generation.apply(preview_id, job_id=job_id))
            return await mask_generation.apply(preview_id)

        @mcp.tool(annotations=read_only)
        async def prepare_point_cloud_import(
                chunk_key: int, path: str, epsg: Annotated[int, Field(ge=1, le=999999)] | None = None,
                load_colors: bool = True, calculate_normals: bool = True) -> dict[str, object]:
            """Preview importing a LAS, LAZ, E57 or PLY point cloud without changing Metashape. Reads the point count from LAS/LAZ/PLY headers, lists existing clouds in the chunk, checks the optional EPSG and stores a file fingerprint."""
            validated = await validate_paths(path)
            return await point_cloud_imports.prepare(
                chunk_key, validated["paths"][0], epsg, load_colors, calculate_normals)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def import_point_cloud(preview_id: str, background: bool = False) -> dict[str, object]:
            """Import the previewed point cloud as a new asset (existing clouds are kept) if the file and the chunk's clouds are unchanged. Creates a restore point first and checks the point count against the header. Save the project manually afterwards. background=true runs it as a job (poll wait_job for operation.percent)."""
            jobs.assert_idle()
            preview = point_cloud_imports.load(preview_id)
            await validate_paths(preview["path"])
            if background:
                return background_job("point_cloud_import", preview.get("chunk_key"),
                                      lambda job_id: point_cloud_imports.apply(preview_id, job_id=job_id))
            return await point_cloud_imports.apply(preview_id)

        @mcp.tool()
        async def prepare_model_import(
                chunk_key: int, path: str, manifest_path: str | None = None,
                transfer_texture: bool = True,
                texture_size: Annotated[int, Field(ge=1024, le=32768)] = 8192,
                coordinate_tolerance_ratio: Annotated[float, Field(gt=0, le=0.1)] = 0.001
                ) -> dict[str, object]:
            """Plan a coordinate-verified OBJ/PLY/STL/FBX/GLB reimport from export_model. Requires its model-coordinate-manifest.json, the same saved project and exact chunk key. Verifies file fingerprint, CRS, chunk transform and source bounds, then plans a working-copy import. transfer_texture bakes the source model texture onto the imported geometry."""
            jobs.assert_idle()
            if texture_size & (texture_size - 1):
                raise ValueError("texture_size должен быть степенью двойки.")
            validated = await validate_paths(path)
            if manifest_path is not None:
                validated_manifest = await validate_paths(manifest_path)
                manifest_path = validated_manifest["paths"][0]
            inspected = inspect_reimport(validated["paths"][0], manifest_path)
            parameters = {
                **inspected,
                "transfer_texture": transfer_texture,
                "texture_size": texture_size,
                "coordinate_tolerance_ratio": coordinate_tolerance_ratio,
            }
            return await jobs.prepare_model_import(chunk_key, parameters)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_model_import(job_id: str) -> dict[str, object]:
            """Run the reviewed model reimport on a new chunk copy. Coordinate mismatch fails the job and cannot replace the source; inspect the result, then explicitly accept_job_result or reject_job_result."""
            return jobs.start(job_id, "model_import")

        @mcp.tool(annotations=read_only)
        async def get_reference_summary(chunk_key: int) -> dict[str, object]:
            """Read chunk/camera/marker coordinate systems and count available reference positions."""
            return await host_call("get_reference_summary", chunk_key=chunk_key)

        @mcp.tool(annotations=read_only)
        async def get_reference_error_statistics(chunk_key: int) -> dict[str, object]:
            """Calculate enabled camera/marker reference position residuals and list the worst ten."""
            return await host_call("get_reference_error_statistics", timeout=None,
                                   chunk_key=chunk_key)

        @mcp.tool(annotations=read_only)
        async def list_geoids() -> dict[str, object]:
            """List geoid grids currently registered in Metashape. This does not add or remove files."""
            return await host_call("list_geoids")

        @mcp.tool(annotations=read_only)
        async def list_camera_references(chunk_key: int,
                                         offset: Annotated[int, Field(ge=0)] = 0,
                                         limit: Annotated[int, Field(ge=1, le=100)] = 40
                                         ) -> dict[str, object]:
            """List camera reference positions, rotations, accuracies and enabled state."""
            return await host_call("list_camera_references", chunk_key=chunk_key,
                                   offset=offset, limit=limit)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_chunk_coordinate_system(
                chunk_key: int, epsg: Annotated[int, Field(ge=1, le=999999)],
                apply_to_cameras: bool = True, apply_to_markers: bool = True) -> dict[str, object]:
            """Assign an EPSG CRS without reprojecting reference values; update transform when enabled references exist."""
            jobs.assert_idle()
            return await host_call("set_chunk_coordinate_system", chunk_key=chunk_key, epsg=epsg,
                                   apply_to_cameras=apply_to_cameras,
                                   apply_to_markers=apply_to_markers)

        @mcp.tool(annotations=read_only)
        async def preview_reference_import(
                chunk_key: int, path: str,
                columns: Annotated[str, Field(min_length=1, max_length=32)] = "nxyz",
                delimiter: Annotated[str, Field(min_length=1, max_length=1)] = ",",
                skip_rows: Annotated[int, Field(ge=0, le=1000)] = 0,
                epsg: Annotated[int, Field(ge=1, le=999999)] | None = None,
                items: Literal["cameras", "markers"] = "cameras",
                create_markers: bool = False) -> dict[str, object]:
            """Validate a reference CSV, match labels and persist a file fingerprint without changing Metashape."""
            validated = await validate_paths(path)
            return await reference_imports.prepare(
                chunk_key, validated["paths"][0], columns, delimiter,
                skip_rows, epsg, items, create_markers)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def import_reference(preview_id: str) -> dict[str, object]:
            """Import an unchanged, valid reference CSV using a previous preview ID and create a restore point."""
            jobs.assert_idle()
            preview = reference_imports._load(preview_id)
            await validate_paths(preview["path"])
            return await reference_imports.apply(preview_id)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_reference(
                chunk_key: int, directory: str,
                items: Literal["cameras", "markers"] = "cameras",
                columns: Annotated[str, Field(min_length=1, max_length=64)] | None = None,
                delimiter: Annotated[str, Field(min_length=1, max_length=1)] = ",",
                precision: Annotated[int, Field(ge=0, le=15)] = 8) -> dict[str, object]:
            """Export typed reference data to a new registered CSV directory under an allowed root."""
            jobs.assert_idle()
            validated = await validate_paths(directory)
            selected_columns = columns or (
                "noxyzXYZabcABC" if items == "cameras" else "noxyzXYZ")
            allowed = set("noxyzXYZabcABC" if items == "cameras" else "noxyzXYZ")
            if ("n" not in selected_columns
                    or len(set(selected_columns)) != len(selected_columns)
                    or any(value not in allowed for value in selected_columns)):
                raise ValueError("columns содержит поля, недопустимые для выбранного типа reference.")
            result = await host_call(
                "export_reference", timeout=None, chunk_key=chunk_key,
                directory=validated["paths"][0], items=items, columns=selected_columns,
                delimiter=delimiter, precision=precision)
            point = result.get("restore_point")
            if point:
                registered = await host_call(
                    "restore_point_register_outputs", timeout=None,
                    restore_point_id=point["restore_point_id"],
                    paths=[result["output_directory"]])
                result["generated_outputs"] = registered["generated_outputs"]
            return result

        async def run_export(kind, chunk_key, directory, options, background=False):
            jobs.assert_idle()
            validated = await validate_paths(directory)
            destination_free_bytes = None
            if kind == "tiled_model":
                destination_free_bytes = shutil.disk_usage(validated["paths"][0]).free
                if destination_free_bytes < 1024 ** 3:
                    raise ValueError("Для экспорта тайловой модели нужно не менее 1 ГБ "
                                     "свободного места в папке назначения.")

            async def work(job_id=None):
                result = await host_call("asset_export", timeout=None, job_id=job_id,
                                         chunk_key=chunk_key, directory=validated["paths"][0],
                                         kind=kind, options=options)
                point = result.get("restore_point")
                if point:
                    registered = await host_call(
                        "restore_point_register_outputs", timeout=None,
                        restore_point_id=point["restore_point_id"], paths=[result["output_directory"]])
                    result["generated_outputs"] = registered["generated_outputs"]
                if destination_free_bytes is not None:
                    result["destination_free_bytes_before"] = destination_free_bytes
                return result
            if background:
                return background_job("export_" + kind, chunk_key, work)
            return await work()

        def background_job(kind, chunk_key, work):
            job = jobs.start_host_job(kind, chunk_key, kind, work)
            return {**job, "note": "Идёт в фоне: следите wait_job/get_job_status (operation.percent); "
                                   "результат — в assets. Посреди операции не отменяется."}

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_model(
                chunk_key: int, directory: str,
                format: Literal["obj", "ply", "stl", "fbx", "glb"] = "obj",
                model_key: int | None = None, save_texture: bool = True, save_uv: bool = True,
                save_normals: bool = True, save_colors: bool = True, clip_to_boundary: bool = True,
                epsg: Annotated[int, Field(ge=1, le=999999)] | None = None, background: bool = False) -> dict[str, object]:
            """Export the active (or model_key) model into a new folder under an allowed root. Also writes model-coordinate-manifest.json for coordinate-verified reimport; use clip_to_boundary=false for that workflow. Texture is written only if the model has one (GLB embeds it). background=true runs it as a job: returns job_id at once, poll wait_job for operation.percent; not cancellable midway."""
            return await run_export("model", chunk_key, directory, {
                "format": format, "model_key": model_key, "save_texture": save_texture, "save_uv": save_uv,
                "save_normals": save_normals, "save_colors": save_colors,
                "clip_to_boundary": clip_to_boundary, "epsg": epsg}, background)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_point_cloud(
                chunk_key: int, directory: str, format: Literal["las", "laz", "e57", "ply"] = "laz",
                point_cloud_key: int | None = None, save_point_color: bool = True,
                save_point_classification: bool = True, classes: list[int] | None = None,
                clip_to_boundary: bool = True,
                epsg: Annotated[int, Field(ge=1, le=999999)] | None = None, background: bool = False) -> dict[str, object]:
            """Export one point cloud (active or point_cloud_key) into a new folder under an allowed root, optionally only the listed classes. background=true runs it as a job: returns job_id at once, poll wait_job for operation.percent; not cancellable midway."""
            return await run_export("point_cloud", chunk_key, directory, {
                "format": format, "point_cloud_key": point_cloud_key, "save_point_color": save_point_color,
                "save_point_classification": save_point_classification, "classes": classes,
                "clip_to_boundary": clip_to_boundary, "epsg": epsg}, background)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_tiled_model(
                chunk_key: int, directory: str,
                format: Literal["cesium", "slpk", "osgb"] = "cesium",
                tiled_model_key: int | None = None,
                epsg: Annotated[int, Field(ge=1, le=999999)] | None = None,
                clip_to_boundary: bool = True,
                tileset_version: Literal["1.0", "1.1"] = "1.0",
                model_compression: bool = True,
                use_tileset_transform: bool = True,
                screen_space_error: Annotated[float, Field(gt=0, le=1024)] = 16,
                folder_depth: Annotated[int, Field(ge=0, le=32)] = 5,
                background: bool = False) -> dict[str, object]:
            """Export one exact/active tiled model as Cesium 3D Tiles (.3tz), SLPK or OSGB into a fresh registered directory. SLPK requires an authoritative CRS. Cesium-only options are ignored for other formats. Boundary clipping is reported. background=true exposes progress through wait_job/get_job_status."""
            return await run_export("tiled_model", chunk_key, directory, {
                "format": format, "tiled_model_key": tiled_model_key, "epsg": epsg,
                "clip_to_boundary": clip_to_boundary,
                "tileset_version": tileset_version, "model_compression": model_compression,
                "use_tileset_transform": use_tileset_transform,
                "screen_space_error": screen_space_error, "folder_depth": folder_depth,
            }, background)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_cameras(
                chunk_key: int, directory: str,
                format: Literal["xml", "colmap", "bundler", "opk", "nvm", "fbx"] = "xml",
                save_points: bool = True, save_markers: bool = True, save_images: bool = False,
                epsg: Annotated[int, Field(ge=1, le=999999)] | None = None, background: bool = False) -> dict[str, object]:
            """Export camera orientations (and optionally tie points and markers) into a new folder under an allowed root. colmap writes cameras.txt, images.txt and points3D.txt for COLMAP / Gaussian Splatting tools; save_images also writes the photos (slow, large). background=true runs it as a job: returns job_id at once, poll wait_job for operation.percent; not cancellable midway."""
            return await run_export("cameras", chunk_key, directory, {
                "format": format, "save_points": save_points, "save_markers": save_markers,
                "save_images": save_images, "epsg": epsg}, background)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_sensor_calibration(
                chunk_key: int, sensor_key: int, directory: str,
                source: Literal["adjusted", "initial"] = "adjusted") -> dict[str, object]:
            """Save one sensor calibration as a new XML file under an allowed root. source=adjusted exports the solved calibration; source=initial exports user_calib. The output is registered for reversible cleanup."""
            return await run_export("sensor_calibration", chunk_key, directory,
                                    {"sensor_key": sensor_key, "source": source})

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def render_views(
                camera_chunk_key: int, camera_keys: Annotated[list[int], Field(min_length=1, max_length=8)],
                chunk_keys: Annotated[list[int], Field(min_length=1, max_length=6)], directory: str,
                scale: Annotated[float, Field(gt=0.05, le=1)] = 0.25, background: bool = False) -> dict[str, object]:
            """Render the active models of chunk_keys from the viewpoints of real cameras of camera_chunk_key (chunks must be aligned to each other), plus a downscaled copy of each photo, into a new folder under an allowed root. For side-by-side visual comparison of reconstruction quality. background=true runs it as a job: returns job_id at once, poll wait_job for operation.percent; not cancellable midway."""
            return await run_export("renders", camera_chunk_key, directory, {
                "camera_keys": camera_keys, "chunk_keys": chunk_keys, "scale": scale}, background)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_report(
                chunk_key: int, directory: str, title: Annotated[str, Field(max_length=200)] = "",
                description: Annotated[str, Field(max_length=2000)] = "",
                include_system_info: bool = True, background: bool = False) -> dict[str, object]:
            """Write Metashape's own PDF processing report for the chunk into a new folder under an allowed root. background=true runs it as a job: returns job_id at once, poll wait_job for operation.percent; not cancellable midway."""
            return await run_export("report", chunk_key, directory, {
                "title": title, "description": description, "include_system_info": include_system_info}, background)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_check_points(chunk_key: int, marker_keys: list[int], check: bool = True) -> dict[str, object]:
            """Turn markers with reference coordinates into check points (check=true: excluded from georeferencing and optimization, error stays an independent accuracy estimate) or back into control points. Updates the chunk transform; creates a restore point."""
            jobs.assert_idle()
            if not marker_keys or len(set(marker_keys)) != len(marker_keys):
                raise ValueError("marker_keys должен быть непустым и без повторов.")
            return await host_call("set_check_points", timeout=None, chunk_key=chunk_key,
                                   marker_keys=marker_keys, check=check)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def export_shapes(
                chunk_key: int, directory: str, format: Literal["shp", "dxf", "geojson", "kml"] = "shp",
                save_points: bool = True, save_polylines: bool = True, save_polygons: bool = True,
                epsg: Annotated[int, Field(ge=1, le=999999)] | None = None, background: bool = False) -> dict[str, object]:
            """Export the chunk's shapes into a new folder under an allowed root. GeoJSON/KML need a geographic coordinate system (refused for local chunks before Metashape is called). background=true runs it as a job: returns job_id at once, poll wait_job for operation.percent; not cancellable midway."""
            return await run_export("shapes", chunk_key, directory, {
                "format": format, "save_points": save_points, "save_polylines": save_polylines,
                "save_polygons": save_polygons, "epsg": epsg}, background)

        @mcp.tool(annotations=read_only)
        async def prepare_shapes_import(chunk_key: int, path: str,
                                        boundary_type: Literal["none", "outer", "inner"] = "none") -> dict[str, object]:
            """Preview importing SHP/DXF/GeoJSON/KML shapes without changing Metashape: checks sidecar files, fingerprints them and reports existing shapes. outer/inner boundaries later clip model and cloud exports."""
            validated = await validate_paths(path)
            return await shape_imports.prepare(chunk_key, validated["paths"][0], boundary_type)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def import_shapes(preview_id: str) -> dict[str, object]:
            """Import previewed shapes if the files and shape count are unchanged (existing shapes kept). Creates a restore point."""
            jobs.assert_idle()
            preview = shape_imports.load(preview_id)
            await validate_paths(preview["path"])
            return await shape_imports.apply(preview_id)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def add_marker_projections(
                chunk_key: int, label: Annotated[str, Field(min_length=1, max_length=100)],
                projections: Annotated[list[dict[str, float]], Field(min_length=2, max_length=50)]
        ) -> dict[str, object]:
            """Create a marker from pixel coordinates on aligned cameras: projections = [{"camera_key": int, "x": px, "y": px}, ...] in the photo's own pixel grid. Metashape triangulates it; the result gives the reprojection error per camera (a large one means a mis-picked point). Creates a restore point."""
            jobs.assert_idle()
            cleaned = []
            for item in projections:
                if set(item) != {"camera_key", "x", "y"}:
                    raise ValueError("Каждая проекция: camera_key, x, y.")
                cleaned.append({"camera_key": int(item["camera_key"]), "x": float(item["x"]), "y": float(item["y"])})
            if len({item["camera_key"] for item in cleaned}) != len(cleaned):
                raise ValueError("Одна камера указана дважды.")
            return await host_call("add_marker_projections", timeout=None, chunk_key=chunk_key,
                                   label=label, projections=cleaned)

        @mcp.tool(annotations=read_only)
        async def compare_chunk_markers(reference_chunk_key: int, chunk_key: int,
                                        labels: list[str] | None = None) -> dict[str, object]:
            """Read-only: distance between same-name markers of two chunks in the common project coordinates (after chunk alignment). Useful as an independent check of an alignment."""
            return await host_call("compare_chunk_markers", timeout=None, reference_chunk_key=reference_chunk_key,
                                   chunk_key=chunk_key, labels=labels)

        @mcp.tool(annotations=read_only)
        async def get_region(chunk_key: int) -> dict[str, object]:
            """Read the reconstruction region (bounding box): center, size and 3x3 rotation in chunk coordinates."""
            return await host_call("get_region", chunk_key=chunk_key)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_region(chunk_key: int,
                             center: Annotated[list[float], Field(min_length=3, max_length=3)] | None = None,
                             size: Annotated[list[float], Field(min_length=3, max_length=3)] | None = None,
                             rotation: Annotated[list[float], Field(min_length=9, max_length=9)] | None = None
                             ) -> dict[str, object]:
            """Change the reconstruction region (any of center, size, row-major 3x3 rotation, in chunk coordinates). Returns before/after; creates a restore point."""
            jobs.assert_idle()
            if center is None and size is None and rotation is None:
                raise ValueError("Укажите center, size или rotation.")
            if size is not None and any(value <= 0 for value in size):
                raise ValueError("Размеры области должны быть больше нуля.")
            return await host_call("set_region", chunk_key=chunk_key, center=center, size=size, rotation=rotation)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def reset_region(chunk_key: int) -> dict[str, object]:
            """Reset the reconstruction region to Metashape's default. Returns before/after; creates a restore point."""
            jobs.assert_idle()
            return await host_call("reset_region", chunk_key=chunk_key)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_marker_reference(
                chunk_key: int, marker_key: int,
                location: Annotated[list[float], Field(min_length=3, max_length=3)],
                accuracy: Annotated[list[float], Field(min_length=3, max_length=3)],
                enabled: bool = True) -> dict[str, object]:
            """Set one marker's reference coordinates and positive XYZ accuracies by exact key."""
            jobs.assert_idle()
            if any(not math.isfinite(value) for value in location + accuracy):
                raise ValueError("Координаты и accuracy должны быть конечными числами.")
            if any(value <= 0 for value in accuracy):
                raise ValueError("Все значения accuracy должны быть больше нуля.")
            return await host_call("set_marker_reference", chunk_key=chunk_key,
                                   marker_key=marker_key, location=location,
                                   accuracy=accuracy, enabled=enabled)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_cameras_enabled(
                chunk_key: int,
                camera_keys: Annotated[list[int], Field(min_length=1, max_length=10000)],
                enabled: bool) -> dict[str, object]:
            """Enable or disable an explicit set of camera keys without deleting photos."""
            jobs.assert_idle()
            if len(set(camera_keys)) != len(camera_keys):
                raise ValueError("camera_keys содержит повторы.")
            return await host_call("set_cameras_enabled", chunk_key=chunk_key,
                                   camera_keys=camera_keys, enabled=enabled)

        @mcp.tool(annotations=read_only)
        async def list_sensors(chunk_key: int, offset: Annotated[int, Field(ge=0)] = 0,
                               limit: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, object]:
            """List sensors, image dimensions, physical pixel size and current calibration."""
            return await host_call("list_sensors", chunk_key=chunk_key, offset=offset, limit=limit)

        @mcp.tool(annotations=read_only)
        async def get_alignment_statistics(chunk_key: int) -> dict[str, object]:
            """Calculate alignment counts and RMS/max tie-point reprojection error in pixels."""
            return await host_call("get_alignment_statistics", timeout=None, chunk_key=chunk_key)

        @mcp.tool(annotations=read_only)
        async def get_alignment_diagnostics(chunk_key: int) -> dict[str, object]:
            """Read alignment, camera-path extent/steps, relative gaps, unaligned runs and GCP/check-point errors. Relative gaps are evidence for review, not a universal drift threshold."""
            return await host_call("get_alignment_diagnostics", timeout=None, chunk_key=chunk_key)

        @mcp.tool(annotations=read_only)
        async def list_markers(chunk_key: int, offset: Annotated[int, Field(ge=0)] = 0,
                               limit: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, object]:
            """List markers, positions, references, projection counts and marker reprojection errors."""
            return await host_call("list_markers", chunk_key=chunk_key, offset=offset, limit=limit)

        @mcp.tool(annotations=read_only)
        async def list_scalebars(chunk_key: int, offset: Annotated[int, Field(ge=0)] = 0,
                                limit: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, object]:
            """List scale bars with endpoints, reference distance, accuracy and estimated distance."""
            return await host_call("list_scalebars", chunk_key=chunk_key, offset=offset, limit=limit)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def create_scalebar(chunk_key: int, marker0_key: int, marker1_key: int,
                                  distance: Annotated[float, Field(gt=0)],
                                  accuracy: Annotated[float, Field(gt=0)] = 0.001) -> dict[str, object]:
            """Create a scale constraint in metres between two marker keys. Save the project explicitly."""
            jobs.assert_idle()
            return await host_call("create_scalebar", chunk_key=chunk_key, marker0_key=marker0_key,
                                   marker1_key=marker1_key, distance=distance, accuracy=accuracy)

        @mcp.tool(annotations=read_only)
        async def get_metashape_log(limit: Annotated[int, Field(ge=1, le=200)] = 40) -> dict[str, object]:
            """Read the latest lines from the actual Metashape console (up to 32 KiB of UTF-8)."""
            return await host_call("get_metashape_log", limit=limit)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def create_restore_point(label: Annotated[str, Field(min_length=1, max_length=255)] = "manual"
                                       ) -> dict[str, object]:
            """Save the project and create a lightweight restore point using Metashape's versioned PSX data."""
            jobs.assert_idle()
            return await host_call("restore_point_create", timeout=None, label=label)

        @mcp.tool(annotations=read_only)
        async def list_restore_points(offset: Annotated[int, Field(ge=0)] = 0,
                                      limit: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, object]:
            """List persistent project restore points with IDs, reasons and original project paths."""
            return await host_call("restore_points_list", offset=offset, limit=limit)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=False))
        async def restore_point(restore_point_id: str) -> dict[str, object]:
            """Restore a project point. A safety point of the current state is created first."""
            jobs.assert_idle()
            return await host_call("restore_point_restore", timeout=None,
                                   restore_point_id=restore_point_id)

        @mcp.tool(annotations=read_only)
        async def list_quarantined_outputs(offset: Annotated[int, Field(ge=0)] = 0,
                                           limit: Annotated[int, Field(ge=1, le=100)] = 40
                                           ) -> dict[str, object]:
            """List reversible exported directories moved to quarantine, including current state and paths."""
            return await host_call("quarantined_outputs_list", offset=offset, limit=limit)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def restore_quarantined_output(quarantine_id: str) -> dict[str, object]:
            """Move one quarantined MCP export back to its original path without overwriting anything."""
            jobs.assert_idle()
            return await host_call("quarantined_output_restore", timeout=None,
                                   quarantine_id=quarantine_id)

        @mcp.tool(annotations=read_only)
        async def list_user_scripts() -> dict[str, object]:
            """List Python files in the UI-approved folder, including hashes and approval state."""
            return await host_call("user_scripts_list", timeout=None)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=False))
        async def run_user_script(path: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
            """Run an approved Python file inside Metashape after explicit UI confirmation and project save."""
            jobs.assert_idle()
            return await host_call("user_script_run", timeout=None, path=path,
                                   arguments=arguments or {})

        @mcp.tool()
        async def prepare_image_quality(chunk_key: int) -> dict[str, object]:
            """Preview eligible photos in the selected chunk and return a job_id. No computation yet."""
            return await jobs.prepare(chunk_key)

        @mcp.tool()
        async def start_image_quality(job_id: str) -> dict[str, object]:
            """Measure full-image sharpness for a prepared job. Repeat the same ID to retrieve its state."""
            return jobs.start(job_id, "image_quality")

        @mcp.tool()
        async def prepare_camera_alignment(chunk_key: int, downscale: Literal[0, 1, 2, 4, 8] = 1,
                                           keypoint_limit: Annotated[int, Field(ge=0, le=2147483647)] = 40000,
                                           tiepoint_limit: Annotated[int, Field(ge=0, le=2147483647)] = 4000,
                                           filter_mask: bool = False, mask_tiepoints: bool = False
                                           ) -> dict[str, object]:
            """Preview matching and alignment of enabled photos on a new chunk copy (masks are copied too). No project edits. filter_mask ignores masked areas when detecting key points; mask_tiepoints also removes tie points in masked areas. The plan reports cameras_with_masks."""
            return await jobs.prepare_alignment(chunk_key, downscale, keypoint_limit, tiepoint_limit,
                                                filter_mask, mask_tiepoints)

        @mcp.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                    idempotent_hint=True))
        async def start_camera_alignment(job_id: str) -> dict[str, object]:
            """Execute the reviewed plan once: duplicate photos, match, align. The user saves the project."""
            return jobs.start(job_id, "alignment")

        @mcp.tool()
        async def prepare_reconstruction(chunk_key: int, downscale: Literal[1, 2, 4, 8, 16] = 4,
                                         faces: Literal["low", "medium", "high"] = "medium") -> dict[str, object]:
            """Plan depth maps and a mesh on a new chunk using enabled aligned cameras."""
            return await jobs.prepare_reconstruction(chunk_key, downscale, faces)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_reconstruction(job_id: str) -> dict[str, object]:
            """Build depth maps then a mesh on the planned copy. Save the project in Metashape."""
            return jobs.start(job_id, "reconstruction")

        @mcp.tool()
        async def prepare_texture_export(chunk_key: int, directory: str,
                                         texture_size: Literal[1024, 2048, 4096, 8192] = 4096) -> dict[str, object]:
            """Plan UV mapping, texture and OBJ export on a copy of the active mesh to a new folder under directory."""
            await validate_paths(directory)
            return await jobs.prepare_texture_export(chunk_key, texture_size, directory)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_texture_export(job_id: str) -> dict[str, object]:
            """Run the texture and OBJ export plan. Get file paths from job status."""
            return jobs.start(job_id, "texture_export")

        @mcp.tool()
        async def prepare_camera_optimization(chunk_key: int, fit_principal_point: bool = True,
                                              fit_skew: bool = False,
                                              adaptive_fitting: bool = False) -> dict[str, object]:
            """Plan camera optimization on a new chunk copy using the current tie points and scale constraints."""
            return await jobs.prepare_camera_optimization(
                chunk_key, fit_principal_point, fit_skew, adaptive_fitting)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_camera_optimization(job_id: str) -> dict[str, object]:
            """Optimize calibration and camera poses on the planned copy. Read status and logs by job ID."""
            return jobs.start(job_id, "camera_optimization")

        @mcp.tool()
        async def prepare_tie_point_filtering(
                chunk_key: int, passes: Annotated[list[dict[str, object]], Field(min_length=1, max_length=6)],
                max_selected_fraction: Annotated[float, Field(gt=0, le=0.9)] = 0.30,
                fit_principal_point: bool = True, fit_skew: bool = False,
                adaptive_fitting: bool = False) -> dict[str, object]:
            """Plan gradual tie-point filtering on a new chunk copy. Each pass is {criterion, threshold, optimize?}; criterion is reconstruction_uncertainty, projection_accuracy or reprojection_error. The plan reports the selected count/fraction and error metrics without changing the source. At execution every pass recalculates the selection and refuses to exceed max_selected_fraction (default 30%)."""
            return await jobs.prepare_tie_point_filtering(
                chunk_key, passes, max_selected_fraction, fit_principal_point, fit_skew,
                adaptive_fitting)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_tie_point_filtering(job_id: str) -> dict[str, object]:
            """Run the reviewed tie-point filtering passes on their working copy, optimizing cameras between passes when requested. The original chunk is unchanged until accept_job_result or auto-accept."""
            return jobs.start(job_id, "tie_point_filtering")

        @mcp.tool()
        async def prepare_sensor_calibration(
                chunk_key: int, sensor_key: int, calibration_path: str | None = None,
                sensor_type: Literal["frame", "fisheye", "spherical", "cylindrical"] | None = None,
                fixed: bool | None = None,
                fixed_params: list[Literal["f", "cx", "cy", "b1", "b2", "k1", "k2", "k3",
                                           "k4", "p1", "p2"]] | None = None
                ) -> dict[str, object]:
            """Plan explicit changes to one sensor on a new chunk copy: load an XML initial calibration, change projection type, fix/unfix the whole calibration, or replace the exact fixed-parameter list. At least one change is required; the source chunk is not modified."""
            resolved = None
            if calibration_path is not None:
                resolved = (await validate_paths(calibration_path))["paths"][0]
            return await jobs.prepare_sensor_calibration(
                chunk_key, sensor_key, resolved, sensor_type, fixed, fixed_params)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_sensor_calibration(job_id: str) -> dict[str, object]:
            """Apply the reviewed sensor-calibration changes to their working copy. The original chunk remains unchanged until accept_job_result or auto-accept."""
            return jobs.start(job_id, "sensor_calibration")

        @mcp.tool()
        async def prepare_camera_reference_exif(
                chunk_key: int, epsg: Literal[4326],
                fallback_accuracy_m: Annotated[list[float], Field(min_length=3, max_length=3)],
                load_rotation: bool = False,
                rotation_accuracy_deg: Annotated[list[float], Field(min_length=3, max_length=3)] | None = None,
                load_accuracy: bool = True, replace_existing: bool = False,
                enable_locations: bool = True, enable_rotations: bool = False,
                update_transform: bool = False) -> dict[str, object]:
            """Plan camera-coordinate loading from photo EXIF/XMP on a new chunk copy. epsg=4326 and a positive XYZ fallback accuracy in metres are mandatory. Existing references are preserved unless replace_existing=true. Rotation needs an explicit angular accuracy; transform update is opt-in."""
            return await jobs.prepare_camera_reference_exif(
                chunk_key, epsg, fallback_accuracy_m, load_rotation, rotation_accuracy_deg,
                load_accuracy, replace_existing, enable_locations, enable_rotations,
                update_transform)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_camera_reference_exif(job_id: str) -> dict[str, object]:
            """Load the reviewed EXIF/XMP camera references on their working copy and report actual coordinates, accuracies, rotations and warnings. The source chunk stays unchanged until acceptance."""
            return jobs.start(job_id, "camera_reference_exif")

        @mcp.tool()
        async def prepare_reference_crs_transform(
                chunk_key: int,
                source_epsg: Annotated[int, Field(ge=1, le=999999)],
                target_epsg: Annotated[int, Field(ge=1, le=999999)],
                transform_cameras: bool = True, transform_markers: bool = True,
                set_chunk_crs: bool = False, update_transform: bool = False
                ) -> dict[str, object]:
            """Plan an actual camera/marker reference-coordinate transformation on a new chunk copy. Source and target EPSG are explicit; rotations are refused, accuracies stay in metres, chunk CRS and updateTransform are opt-in."""
            return await jobs.prepare_reference_crs_transform(
                chunk_key, source_epsg, target_epsg, transform_cameras,
                transform_markers, set_chunk_crs, update_transform)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_reference_crs_transform(job_id: str) -> dict[str, object]:
            """Transform the reviewed reference coordinates on their working copy. The source chunk remains unchanged until explicit acceptance."""
            return jobs.start(job_id, "reference_crs_transform")

        @mcp.tool()
        async def prepare_raster_products(
                chunk_key: int, directory: str,
                resolution: Annotated[float, Field(ge=0)] = 0,
                refine_seamlines: bool = False, classes: list[int] | None = None) -> dict[str, object]:
            """Plan DEM, orthomosaic and two GeoTIFF exports on a chunk copy. Zero resolution lets Metashape choose. classes limits the DEM surface to these point classes (e.g. [2] = ground after classify_ground); needs a dense point cloud."""
            await validate_paths(directory)
            return await jobs.prepare_raster_products(
                chunk_key, directory, resolution, refine_seamlines, classes)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_raster_products(job_id: str) -> dict[str, object]:
            """Build DEM and orthomosaic on the planned copy, then export registered GeoTIFF files."""
            return jobs.start(job_id, "raster_products")

        @mcp.tool()
        async def prepare_marker_detection(
                chunk_key: int,
                target_type: Literal[
                    "circular_12bit", "circular_14bit", "circular_16bit", "circular_20bit"
                ] = "circular_12bit",
                tolerance: Annotated[int, Field(ge=0, le=100)] = 50,
                filter_mask: bool = False) -> dict[str, object]:
            """Plan coded-target detection on a new chunk copy using enabled photos."""
            return await jobs.prepare_marker_detection(
                chunk_key, target_type, tolerance, filter_mask)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_marker_detection(job_id: str) -> dict[str, object]:
            """Detect coded targets on the planned chunk copy without merging into the source chunk."""
            return jobs.start(job_id, "marker_detection")

        @mcp.tool()
        async def prepare_marker_refinement(
                chunk_key: int,
                marker_keys: Annotated[list[int], Field(min_length=1, max_length=10000)]
                ) -> dict[str, object]:
            """Plan image-based refinement for exact marker keys on a new chunk copy."""
            if len(set(marker_keys)) != len(marker_keys):
                raise ValueError("marker_keys содержит повторы.")
            return await jobs.prepare_marker_refinement(chunk_key, marker_keys)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_marker_refinement(job_id: str) -> dict[str, object]:
            """Refine the selected marker projections on the planned copy and report pixel shifts."""
            return jobs.start(job_id, "marker_refinement")

        @mcp.tool()
        async def prepare_point_cloud(
                chunk_key: int,
                point_colors: bool = True,
                point_confidence: bool = True,
                keep_depth: bool = True,
                max_neighbors: Annotated[int, Field(ge=1, le=1000)] = 100,
                uniform_sampling: bool = True,
                points_spacing: Annotated[float, Field(gt=0, le=10000)] = 0.1,
                in_place: bool = False) -> dict[str, object]:
            """Plan point-cloud generation from the active depth maps on a new chunk copy. in_place=true builds in the source chunk instead (no copy, for big projects): verified reversible, the job's restore point makes the previous cloud active again even after saving."""
            return await jobs.prepare_point_cloud(
                chunk_key, point_colors, point_confidence, keep_depth,
                max_neighbors, uniform_sampling, points_spacing, in_place=in_place)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_point_cloud(job_id: str) -> dict[str, object]:
            """Build one non-empty point cloud from reviewed depth maps on the planned copy."""
            return jobs.start(job_id, "point_cloud")

        @mcp.tool()
        async def prepare_point_cloud_cleanup(
                chunk_key: int, point_cloud_key: int,
                selector: Literal["classes", "confidence"],
                point_classes: list[Annotated[int, Field(ge=0, le=127)]] | None = None,
                min_confidence: Annotated[int, Field(ge=0, le=255)] | None = None,
                max_confidence: Annotated[int, Field(ge=0, le=255)] | None = None,
                max_selected_fraction: Annotated[float, Field(gt=0, le=1)] = 0.30
                ) -> dict[str, object]:
            """Preview deletion from the exact active point cloud by whole classes or confidence range. Reports selected count/fraction, refuses an empty result or anything above max_selected_fraction before creating a copy. For confidence, point_classes optionally restricts affected classes; empty means all present classes."""
            return await jobs.prepare_point_cloud_cleanup(
                chunk_key, point_cloud_key, selector, point_classes,
                min_confidence, max_confidence, max_selected_fraction)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_point_cloud_cleanup(job_id: str) -> dict[str, object]:
            """Delete the reviewed points only on the working copy, reset filters, update statistics and report before/after counts. The source stays unchanged until explicit acceptance."""
            return jobs.start(job_id, "point_cloud_cleanup")

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def reload_service() -> dict[str, object]:
            """Development only: reload the MCP service inside Metashape with the current source code. Works only when the user enabled it in the status window and no job is running. The connection drops; reconnect after about two seconds."""
            jobs.assert_idle()
            return await host_call("service_reload")

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_active_chunk(chunk_key: int) -> dict[str, object]:
            """Make the chunk active in Metashape. Returns the previous active chunk; save the project manually."""
            jobs.assert_idle()
            return await host_call("set_active_chunk", chunk_key=chunk_key)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        async def duplicate_chunk(
                chunk_key: int, label: Annotated[str, Field(min_length=1, max_length=200)],
                items: list[Literal["tie_points", "depth_maps", "point_clouds", "models", "tiled_models",
                                    "elevations", "orthomosaics", "masks"]] = [],
                keypoints: bool = False) -> dict[str, object]:
            """Copy a chunk with its cameras and markers plus only the listed data items. Creates a restore point; the source chunk is unchanged."""
            jobs.assert_idle()
            if len(set(items)) != len(items):
                raise ValueError("В items есть повторы.")
            return await host_call("chunk_duplicate", timeout=None, chunk_key=chunk_key,
                                   label=label.strip(), items=items, keypoints=keypoints)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def set_chunk_enabled(chunk_key: int, enabled: bool) -> dict[str, object]:
            """Archive (enabled=false) or re-enable a chunk without deleting anything: the chunk and its data stay in the project. Creates a restore point; save the project to keep the change."""
            jobs.assert_idle()
            return await host_call("chunk_set_enabled", timeout=None, chunk_key=chunk_key,
                                   enabled=enabled)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=False))
        async def remove_chunk(chunk_key: int,
                               confirm_label: Annotated[str, Field(min_length=1, max_length=200)]
                               ) -> dict[str, object]:
            """Remove a chunk from a .psx project and save. confirm_label must equal the chunk label exactly. A restore point is created first and the chunk's data folder is moved (same disk, no copy) into .metashape-mcp-quarantine next to the project; restore_point puts the chunk and its data back. Prefer set_chunk_enabled(false) when unsure."""
            jobs.assert_idle()
            return await host_call("chunk_remove", timeout=None, chunk_key=chunk_key,
                                   confirm_label=confirm_label)

        @mcp.tool(annotations=read_only)
        async def get_chunk_compatibility(chunk_keys: list[int]) -> dict[str, object]:
            """Read-only: compare the first chunk (reference) with the others for Align Chunks: aligned cameras, shared markers with projections, shared aligned cameras, coordinate systems and assets; tells which methods (points, markers, cameras) are possible and why not."""
            return await host_call("chunk_compatibility", timeout=None, chunk_keys=chunk_keys)

        @mcp.tool()
        async def prepare_chunk_alignment(
                reference_chunk_key: int, chunk_keys: list[int],
                method: Literal["points", "markers", "cameras"],
                fit_scale: bool = True, downscale: Literal[0, 1, 2, 4, 8] = 1,
                generic_preselection: bool = False, filter_mask: bool = False,
                keypoint_limit: Annotated[int, Field(ge=1000, le=1000000)] = 40000) -> dict[str, object]:
            """Plan Align Chunks without changing Metashape: checks that the method is possible for every chunk (markers: at least 3 shared markers by name, used automatically) and fingerprints the chunks. Only the position of chunk_keys changes; the reference stays."""
            jobs.assert_idle()
            return await jobs.prepare_chunk_alignment(reference_chunk_key, chunk_keys, {
                "method": method, "fit_scale": fit_scale, "downscale": downscale,
                "generic_preselection": generic_preselection, "filter_mask": filter_mask,
                "keypoint_limit": keypoint_limit})

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_chunk_alignment(job_id: str) -> dict[str, object]:
            """Run a planned chunk alignment in the open project after a restore point. Refuses if the chunks changed. Use wait_job/get_job_status."""
            return jobs.start(job_id, "chunk_alignment")

        @mcp.tool()
        async def prepare_chunk_merge(
                chunk_keys: list[int], copy_masks: bool = True, copy_laser_scans: bool = True,
                copy_depth_maps: bool = False, copy_point_clouds: bool = False, copy_models: bool = False,
                copy_tiled_models: bool = False, copy_elevations: bool = False,
                copy_orthomosaics: bool = False, merge_markers: bool = False,
                merge_tiepoints: bool = False, merge_assets: bool = False) -> dict[str, object]:
            """Plan merging the listed chunks (at least two, never all implicitly) into a new chunk without changing Metashape. Shows the chunks, coordinate-system agreement and chosen data to copy."""
            jobs.assert_idle()
            return await jobs.prepare_chunk_merge(chunk_keys, {
                "copy_masks": copy_masks, "copy_laser_scans": copy_laser_scans,
                "copy_depth_maps": copy_depth_maps, "copy_point_clouds": copy_point_clouds,
                "copy_models": copy_models, "copy_tiled_models": copy_tiled_models,
                "copy_elevations": copy_elevations, "copy_orthomosaics": copy_orthomosaics,
                "merge_markers": merge_markers, "merge_tiepoints": merge_tiepoints,
                "merge_assets": merge_assets})

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_chunk_merge(job_id: str) -> dict[str, object]:
            """Run a planned merge after a restore point; the result is one new chunk and the sources stay unchanged. Use wait_job/get_job_status."""
            return jobs.start(job_id, "chunk_merge")

        @mcp.tool(annotations=read_only)
        async def get_model_statistics(chunk_key: int, model_key: int | None = None) -> dict[str, object]:
            """Read-only: faces, vertices, textures, surface area, connected components, open edges and topology problems of all or one model. Volume is given only for a closed surface (no open edges)."""
            return await host_call("get_model_statistics", timeout=None, chunk_key=chunk_key,
                                   model_key=model_key)

        @mcp.tool(annotations=read_only)
        async def get_model_confidence(chunk_key: int, model_key: int | None = None,
                                       bins: Annotated[int, Field(ge=2, le=50)] = 10) -> dict[str, object]:
            """Read-only: distribution of per-vertex confidence of a model (how many depth maps support each vertex), as a histogram in percent of the maximum, like Metashape's Clean Model. Needs a model built from depth maps with vertex confidence."""
            return await host_call("get_model_confidence", timeout=None, chunk_key=chunk_key,
                                   model_key=model_key, bins=bins)

        @mcp.tool()
        async def prepare_model_finishing(chunk_key: int, steps: list[dict[str, object]]) -> dict[str, object]:
            """Plan clean-up of the chunk's active model on a new chunk copy (no project edits; always a copy: saving rewrites model.zip in place, so an in-place edit could not be rolled back). steps run in order, each {"operation": ...}: remove_components {min_faces: polygon-count threshold}, fix_topology {}, close_holes {level: hole size %, default 30}, smooth {strength (default 3), fix_borders (default true), preserve_edges}, decimate {face_count}, remove_low_confidence {max_percent: faces with a vertex below this % of the maximum confidence}, remove_box / crop_box {min: [x,y,z], max: [x,y,z] in chunk coordinates, see get_region}. Returns warnings: close_holes extrapolates surface; geometry changes invalidate textures (decimation drops the atlas), so texture afterwards."""
            jobs.assert_idle()
            return await jobs.prepare_model_finishing(chunk_key, steps)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_model_finishing(job_id: str) -> dict[str, object]:
            """Run a planned model clean-up on the copy; results list faces, vertices, components, open edges and area after every step. Use wait_job/get_job_status."""
            return jobs.start(job_id, "model_finishing")

        @mcp.tool(annotations=read_only)
        async def describe_autopilot_tasks(task: str | None = None) -> dict[str, object]:
            """Read-only: the tasks prepare_autopilot accepts, what each needs and produces, and every parameter with its type, Metashape default and, for enums, the allowed value names (pass them as strings). Use before writing steps; one step = one Metashape task, so a single-step chain runs any task on its own."""
            return await host_call("autopilot_tasks", timeout=None, task=task)

        @mcp.tool()
        async def prepare_autopilot(chunk_key: int, steps: list[dict[str, object]]) -> dict[str, object]:
            """Plan a chain of Metashape processing tasks on a new chunk copy, e.g. [{"task": "MatchPhotos", "parameters": {"downscale": 2}}, {"task": "AlignCameras"}, {"task": "BuildDepthMaps"}, {"task": "BuildModel"}, {"task": "BuildUV"}, {"task": "BuildTexture"}]. Allowed tasks: MatchPhotos, AlignCameras, OptimizeCameras, DetectMarkers, BuildDepthMaps, BuildPointCloud, BuildModel, DecimateModel, CloseHoles, BuildUV, BuildTexture, ClassifyGroundPoints, BuildDem, BuildOrthomosaic. Parameter names are those of Metashape.Tasks (the plan returns every step's effective parameters). No import/export/save: export the result with the export tools afterwards."""
            jobs.assert_idle()
            return await jobs.prepare_autopilot(chunk_key, steps)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_autopilot(job_id: str) -> dict[str, object]:
            """Run a planned autopilot chain on its copy. Checks between steps, stops on the first failure, macOS notification at the end. Use list_jobs / wait_job / get_job_status."""
            return jobs.start(job_id, "autopilot")

        @mcp.tool()
        async def prepare_ground_classification(
                chunk_key: int, max_angle: Annotated[float, Field(gt=0, le=90)] = 10.0,
                max_distance: Annotated[float, Field(gt=0)] = 1.0,
                max_terrain_slope: Annotated[float, Field(gt=0, le=90)] = 10.0,
                cell_size: Annotated[float, Field(gt=0)] = 50.0,
                erosion_radius: Annotated[float, Field(ge=0)] = 0.0,
                keep_existing: bool = False) -> dict[str, object]:
            """Plan ground-point classification of the active dense cloud on a chunk copy (always a copy: saving after an in-place run deletes the previous cloud version, so it could not be rolled back) (Metashape defaults: max_angle 10 deg, max_distance 1 m, max_terrain_slope 10 deg, cell_size 50 m). Result: point counts per class before and after."""
            jobs.assert_idle()
            return await jobs.prepare_ground_classification(chunk_key, False, {
                "max_angle": max_angle, "max_distance": max_distance,
                "max_terrain_slope": max_terrain_slope, "cell_size": cell_size,
                "erosion_radius": erosion_radius, "keep_existing": keep_existing})

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_ground_classification(job_id: str) -> dict[str, object]:
            """Classify ground points on the planned copy."""
            return jobs.start(job_id, "ground_classification")

        @mcp.tool()
        async def prepare_contours(
                chunk_key: int, directory: str, interval: Annotated[float, Field(gt=0)],
                min_value: float = -1e10, max_value: float = 1e10, prevent_intersections: bool = True,
                format: Literal["shp", "geojson", "dxf", "kml"] = "shp") -> dict[str, object]:
            """Plan contour lines from the active DEM on a chunk copy and their export to a new folder under an allowed root. interval is in DEM height units."""
            jobs.assert_idle()
            await validate_paths(directory)
            return await jobs.prepare_contours(chunk_key, directory, {
                "interval": interval, "min_value": min_value, "max_value": max_value,
                "prevent_intersections": prevent_intersections, "format": format})

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        async def start_contours(job_id: str) -> dict[str, object]:
            """Build and export contour lines on the planned copy; the export folder is registered for quarantine."""
            return jobs.start(job_id, "contours")

        async def with_operation(status):
            """Attach the host's progress of the running Metashape operation to a job status."""
            if status.get("state") in ("running", "cancelling"):
                try:
                    service = await host_call("get_service_status", timeout=5)
                except Exception:
                    return status
                operation = service.get("operation")
                if operation and operation.get("job_id") == status.get("job_id"):
                    position = jobs.positions.get(status["job_id"])
                    if position:
                        index, count = position
                        operation = stage_progress(operation, index, count)
                    status["operation"] = operation
            return status

        def decided_job(job_id, states):
            job = jobs.info(job_id)
            if job["kind"] in INPLACE_KINDS or job["kind"] == "image_quality":
                raise ValueError("Это задание работает без копии чанка; решать нечего.")
            if job["state"] not in states:
                raise ValueError(f"Задание в состоянии {job['state']}; нужно: {', '.join(states)}.")
            if (job.get("parameters") or {}).get("in_place"):
                raise ValueError("Задание шло на месте, без копии: принимать нечего. Отменить его "
                                 "можно только через restore_point.")
            if job.get("copy_key") is None:
                raise ValueError("У задания нет рабочей копии.")
            if job.get("decision"):
                raise ValueError(f"По заданию уже принято решение: {job['decision']}. Если его "
                                 "откатили через restore_point, распорядитесь чанками вручную: "
                                 "remove_chunk или set_chunk_enabled.")
            return job

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=False))
        async def accept_job_result(
                job_id: str, source_action: Literal["archive", "remove"] = "archive",
                confirm_source_label: str | None = None) -> dict[str, object]:
            """Keep the result of a finished copy-based job: the working copy takes the source chunk's name and becomes active. source_action=archive (default) disables the source and renames it '<name> · архив' (nothing deleted, no space freed); remove takes it out of the project, its data folder goes to quarantine next to the project (needs confirm_source_label = source chunk name). Saves the project after a restore point."""
            jobs.assert_idle()
            job = decided_job(job_id, ("completed", "completed_with_errors"))
            if source_action == "remove" and confirm_source_label != job["chunk_label"]:
                raise ValueError("Для удаления исходного чанка confirm_source_label должен "
                                 f"совпадать с его именем «{job['chunk_label']}».")
            result = await host_call("chunk_promote", timeout=None, source_key=job["chunk_key"],
                                     copy_key=job["copy_key"], copy_label=job["copy_label"],
                                     source_label=job["chunk_label"], source_action=source_action,
                                     confirm_source_label=confirm_source_label)
            jobs.decide(job_id, f"accepted ({source_action})")
            jobs.record("job_result_accepted", job_id=job_id, source_action=source_action,
                        copy_key=job["copy_key"], source_key=job["chunk_key"])
            return result

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=True))
        async def set_job_auto_accept(job_id: str,
                                      source_action: Literal["archive", "remove"] | None = "archive"
                                      ) -> dict[str, object]:
            """For step-by-step work: when this prepared or running copy-based job completes without errors, accept its result automatically, like accept_job_result (the copy takes the source name; source archived, or removed into quarantine with remove). None switches it off. Failed, cancelled or partly successful jobs are never accepted automatically."""
            return jobs.set_auto_accept(job_id, source_action)

        @mcp.tool(annotations=types.ToolAnnotations(destructive_hint=True, idempotent_hint=False))
        async def reject_job_result(job_id: str) -> dict[str, object]:
            """Discard the working copy of a finished, failed, cancelled or interrupted copy-based job: the copy chunk is removed (only if it still has the job's label) and its data folder goes to quarantine next to the project, freeing the project folder. The source chunk is untouched. Saves the project after a restore point."""
            jobs.assert_idle()
            job = decided_job(job_id, ("completed", "completed_with_errors", "failed",
                                          "cancelled", "interrupted"))
            result = await host_call("chunk_remove", timeout=None, chunk_key=job["copy_key"],
                                     confirm_label=job["copy_label"])
            jobs.decide(job_id, "rejected")
            jobs.record("job_result_rejected", job_id=job_id, copy_key=job["copy_key"])
            return result

        @mcp.tool(annotations=read_only)
        async def list_jobs(limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, object]:
            """Most recent jobs with kind, state and stage, newest first. Reads only the service database, so it works while Metashape is busy."""
            active = jobs.active_job()
            return {"active_job_id": active["job_id"] if active else None, "jobs": jobs.recent(limit)}

        @mcp.tool(annotations=read_only)
        async def get_job_status(job_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                                 limit: Annotated[int, Field(ge=1, le=100)] = 40,
                                 only_unaligned: bool = False) -> dict[str, object]:
            """Get job stage, counts and paginated camera results; optionally show just unaligned cameras. While running, operation shows the Metashape step's percent, time since the last progress report and stalled (no report for 5 min)."""
            return await with_operation(jobs.status(job_id, offset, limit, only_unaligned))

        @mcp.tool(annotations=read_only)
        async def wait_job(job_id: str,
                           timeout_seconds: Annotated[float, Field(ge=0.1, le=300)] = 30
                           ) -> dict[str, object]:
            """Wait efficiently for a job state change or timeout, then return its current status (with operation progress while running)."""
            return await with_operation(await jobs.wait(job_id, timeout_seconds))

        @mcp.tool()
        async def cancel_job(job_id: str) -> dict[str, object]:
            """Cancel a job at the next step boundary; completed work is kept. The Metashape operation running now cannot be stopped from the API without a modal dialog (probe 0.34), so if get_job_status.operation is slow or stalled, ask the user to press "Отмена" in Metashape's progress window."""
            result = jobs.cancel(job_id)
            if result["state"] == "cancelling":
                signal = await host_call("progress_cancel", timeout=10, job_id=job_id)
                result["operation_signalled"] = signal.get("signalled")
            return result

        @mcp.tool(annotations=read_only)
        def get_service_log(cursor: Annotated[int, Field(ge=0)] = 0,
                            limit: Annotated[int, Field(ge=1, le=100)] = 40,
                            job_id: str | None = None) -> dict[str, object]:
            """Read persistent events, optionally for one job. Continue with next_cursor."""
            with journal.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                if cursor > size:
                    raise ValueError("Курсор за пределами журнала.")
                if cursor:
                    stream.seek(cursor - 1)
                    if stream.read(1) != b"\n":
                        raise ValueError("Курсор должен указывать на начало записи.")
                stream.seek(cursor)
                events = []
                for _ in range(1000):
                    line = stream.readline()
                    if not line:
                        break
                    event = json.loads(line)
                    if job_id is None or event.get("job_id") == job_id:
                        events.append(event)
                    if len(events) == limit:
                        break
                return {"events": events, "next_cursor": stream.tell(), "has_more": stream.tell() < size}

        app = mcp.streamable_http_app(host="127.0.0.1", stateless_http=True)
        web_server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        threading.Thread(target=bridge.listen, args=(asyncio.get_running_loop(), web_server),
                         daemon=True).start()
        record("starting", port=port)
        try:
            await web_server.serve(sockets=[listener])
        finally:
            await jobs.close()
            record("stopped")


if __name__ == "__main__":
    asyncio.run(serve(int(sys.argv[1]), Path(sys.argv[2])))
