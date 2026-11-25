"""Autopilot: a chain of Metashape.Tasks applied to a working copy of a chunk.

Only processing tasks are allowed; file import/export, project load/save and chunk
duplication/merging stay with the typed MCP tools that check paths first (Metashape
answers write errors with modal dialogs). Validation of task names and parameter names
happens inside Metashape against task.encode(); this module holds the pure rules.
"""

MAX_STEPS = 20
# Selection is fixed by the job (all enabled cameras of the copy); file output goes through the
# typed export tools that check paths first.
FORBIDDEN_PARAMETERS = ("path", "cameras", "frames", "chunks", "output_folder", "export_blocks",
                        "ambient_occlusion_path")
# task name: (inputs it needs, outputs it produces) — data kinds used to decide what to copy
TASKS = {
    "MatchPhotos": ((), ("matches",)),
    "AlignCameras": (("matches",), ("tie_points",)),
    "OptimizeCameras": (("tie_points",), ()),
    "DetectMarkers": ((), ()),
    "BuildDepthMaps": ((), ("depth_maps",)),
    "BuildPointCloud": (("depth_maps",), ("point_cloud",)),
    "BuildModel": (("depth_maps",), ("model",)),
    "DecimateModel": (("model",), ("model",)),
    "CloseHoles": (("model",), ("model",)),
    "BuildUV": (("model",), ("model",)),
    "BuildTexture": (("model",), ("model",)),
    "ClassifyGroundPoints": (("point_cloud",), ("point_cloud",)),
    "BuildDem": (("point_cloud",), ("elevation",)),
    "BuildOrthomosaic": (("elevation",), ("orthomosaic",)),
    # added in 0.34 after a probe on a copy (test_runs/autopilot-tasks-test.json)
    "TriangulateTiePoints": (("tie_points",), ("tie_points",)),
    "SmoothModel": (("model",), ("model",)),
    "RefineModel": (("model",), ("model",)),
    "ColorizeModel": (("model",), ("model",)),
    "RemoveLighting": (("model",), ("model",)),
    "CalibrateColors": (("model",), ()),
    "CalculatePointNormals": (("point_cloud",), ("point_cloud",)),
    "ColorizePointCloud": (("point_cloud",), ("point_cloud",)),
    "SmoothPointCloud": (("point_cloud",), ("point_cloud",)),
    "FilterPointCloud": (("point_cloud",), ("point_cloud",)),
    "ClassifyPoints": (("point_cloud",), ("point_cloud",)),
    "BuildTiledModel": (("depth_maps",), ("tiled_model",)),
    "BuildSeamlines": (("orthomosaic",), ()),
}
# Tasks whose input follows a parameter: DataSource name -> data kind ("" = photos only).
SOURCE_PARAMETERS = {"source_data", "surface_data"}
# For these the source replaces the default input (geometry is built from it, or colours are
# calibrated against it).
GEOMETRY_SOURCE_TASKS = {"BuildModel", "BuildTiledModel", "BuildDem", "BuildOrthomosaic", "BuildPointCloud",
                         "CalibrateColors"}
SOURCE_KINDS = {"DepthMapsData": "depth_maps", "PointCloudData": "point_cloud", "ModelData": "model",
                "TiePointsData": "tie_points", "ElevationData": "elevation",
                "OrthomosaicData": "orthomosaic", "TiledModelData": "tiled_model", "ImagesData": ""}
COPY_ITEMS = {"tie_points": "TiePointsData", "depth_maps": "DepthMapsData", "point_cloud": "PointCloudData",
              "model": "ModelData", "elevation": "ElevationData", "orthomosaic": "OrthomosaicData",
              "tiled_model": "TiledModelData"}


def step_inputs(step):
    """Data kinds a step reads: the task's default, or the kind named by source_data/surface_data."""
    inputs, _outputs = TASKS[step["task"]]
    for name in SOURCE_PARAMETERS:
        value = (step.get("parameters") or {}).get(name)
        if isinstance(value, str):
            kind = SOURCE_KINDS.get(value.split(".")[-1])
            if kind is None:
                raise ValueError(f"{step['task']}.{name}: источник {value!r} в автопилоте не поддержан. "
                                 f"Можно: {', '.join(SOURCE_KINDS)}.")
            if step["task"] in GEOMETRY_SOURCE_TASKS:
                return (kind,) if kind else ()
            # BuildTexture, ColorizeModel, CalibrateColors...: the source only gives colours
            return inputs + ((kind,) if kind and kind not in inputs else ())
    return inputs


def validate_steps(steps):
    """Return normalized steps [{"task": name, "parameters": {...}}] or raise ValueError."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"steps: от 1 до {MAX_STEPS} шагов.")
    result = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or set(step) - {"task", "parameters"}:
            raise ValueError(f"Шаг {index}: ожидается {{\"task\": ..., \"parameters\": {{...}}}}.")
        task = step.get("task")
        if task not in TASKS:
            raise ValueError(f"Шаг {index}: задача {task!r} не разрешена в автопилоте. "
                             f"Разрешены: {', '.join(TASKS)}.")
        parameters = step.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise ValueError(f"Шаг {index}: parameters должен быть объектом.")
        for name in FORBIDDEN_PARAMETERS:
            if name in parameters:
                raise ValueError(f"Шаг {index}: параметр {name} в автопилоте не задаётся.")
        result.append({"task": task, "parameters": parameters})
    return result


def required_inputs(steps):
    """Data kinds the chain needs from the source chunk (not produced by an earlier step)."""
    produced, needed = set(), []
    for step in steps:
        _default, outputs = TASKS[step["task"]]
        inputs = step_inputs(step)
        for kind in inputs:
            if kind == "matches" and kind not in produced:
                raise ValueError("AlignCameras в автопилоте должен идти после MatchPhotos "
                                 "(в копии чанка нет ключевых точек).")
            if kind not in produced and kind not in needed:
                needed.append(kind)
        produced.update(outputs)
    return needed


def notification_text(kind, state, message):
    titles = {"completed": "готово", "completed_with_errors": "готово с ошибками",
              "failed": "ошибка", "cancelled": "отменено", "interrupted": "прервано"}
    text = f"{kind}: {titles.get(state, state)}"
    return text + (f" — {message[:120]}" if message else "")
