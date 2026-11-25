"""Model clean-up steps for the copy-based model_finishing job.

Validation runs in the external MCP process (no Metashape import); apply_step and
model_figures run inside Metashape on the working copy. Facts from the Metashape Pro 2.2
manual and Python API 2.2.0:
- decimation discards the texture atlas and cannot be undone (work on a copy, texture later);
- close holes replaces photogrammetric surface with extrapolation (reported to the user);
- Model.removeComponents(size) takes a polygon-count threshold, not the GUI percentage;
- Model.closeHoles(level) takes the hole size threshold in percent;
- smoothModel/decimateModel(replace_asset=True) replace the copy's model.
"""

MAX_STEPS = 10
OPERATIONS = {
    "remove_components": {"min_faces": int},
    "fix_topology": {},
    "close_holes": {"level": int},
    "smooth": {"strength": float, "fix_borders": bool, "preserve_edges": bool},
    "decimate": {"face_count": int},
    "remove_low_confidence": {"max_percent": float},
    "remove_box": {"min": list, "max": list},
    "crop_box": {"min": list, "max": list},
}
DEFAULTS = {"smooth": {"strength": 3.0, "fix_borders": True, "preserve_edges": False},
            "close_holes": {"level": 30}}


def validate_steps(steps):
    """Return normalized steps or raise ValueError with a readable reason."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"steps должен содержать от 1 до {MAX_STEPS} шагов.")
    normalized = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or step.get("operation") not in OPERATIONS:
            raise ValueError(f"Шаг {index}: operation должен быть одним из {', '.join(OPERATIONS)}.")
        operation = step["operation"]
        allowed = OPERATIONS[operation]
        unknown = set(step) - set(allowed) - {"operation"}
        if unknown:
            raise ValueError(f"Шаг {index} ({operation}): неизвестные параметры {sorted(unknown)}.")
        result = {"operation": operation, **DEFAULTS.get(operation, {})}
        for name, kind in allowed.items():
            if name in step:
                value = step[name]
                if kind is bool and not isinstance(value, bool):
                    raise ValueError(f"Шаг {index}: {name} должен быть true или false.")
                if kind in (int, float) and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError(f"Шаг {index}: {name} должен быть числом.")
                if kind is list:
                    if (not isinstance(value, list) or len(value) != 3
                            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)):
                        raise ValueError(f"Шаг {index}: {name} — три числа [x, y, z].")
                    result[name] = [float(v) for v in value]
                    continue
                result[name] = kind(value)
            elif name not in result:
                raise ValueError(f"Шаг {index} ({operation}): укажите {name}.")
        if operation == "remove_components" and result["min_faces"] < 1:
            raise ValueError(f"Шаг {index}: min_faces должен быть не меньше 1.")
        if operation == "close_holes" and not 1 <= result["level"] <= 100:
            raise ValueError(f"Шаг {index}: level — процент от 1 до 100.")
        if operation == "smooth" and not 0 < result["strength"] <= 100:
            raise ValueError(f"Шаг {index}: strength должен быть больше 0 и не больше 100.")
        if operation == "remove_low_confidence" and not 0 < result["max_percent"] < 100:
            raise ValueError(f"Шаг {index}: max_percent — от 0 до 100 (процент от максимальной уверенности).")
        if operation in ("remove_box", "crop_box") and any(a >= b for a, b in zip(result["min"], result["max"])):
            raise ValueError(f"Шаг {index}: min должен быть меньше max по всем осям.")
        if operation == "decimate" and result["face_count"] < 100:
            raise ValueError(f"Шаг {index}: face_count должен быть не меньше 100.")
        normalized.append(result)
    return normalized


def plan_warnings(steps, model):
    """Warnings for a plan, given source model figures (faces, textures)."""
    warnings = []
    if any(step["operation"] == "close_holes" for step in steps):
        warnings.append("Закрытие отверстий заменяет фотограмметрическую поверхность экстраполяцией; "
                        "не используйте закрытые участки для измерений.")
    if model.get("textures") and any(step["operation"] in ("decimate", "smooth", "close_holes",
                                                           "remove_components", "fix_topology")
                                     for step in steps):
        warnings.append("Изменение геометрии делает текстуру недействительной (при упрощении Metashape "
                        "удаляет атлас). Текстурируйте модель после доработки.")
    for step in steps:
        if step["operation"] == "decimate" and model.get("faces") and step["face_count"] >= model["faces"]:
            warnings.append(f"decimate: целевое число граней {step['face_count']} не меньше текущего "
                            f"({model['faces']}), модель не упростится.")
    return warnings


def model_figures(model):
    stats = model.statistics()
    return {"faces": len(model.faces), "vertices": len(model.vertices),
            "components": int(stats.components), "open_edges": int(stats.open_edges),
            "area": None if model.area() is None else float(model.area()), "textures": len(model.textures)}


def apply_step(chunk, step, progress=None):
    """progress: optional Callable[[float], None]; only smoothModel/decimateModel accept one
    (Model.closeHoles/fixTopology/removeComponents have no progress parameter in API 2.2.0)."""
    extra = {} if progress is None else {"progress": progress}
    operation = step["operation"]
    model = chunk.model
    if operation == "remove_components":
        model.removeComponents(step["min_faces"])
    elif operation == "fix_topology":
        model.fixTopology()
    elif operation == "close_holes":
        model.closeHoles(level=step["level"])
    elif operation == "smooth":
        chunk.smoothModel(strength=step["strength"], fix_borders=step["fix_borders"],
                          preserve_edges=step["preserve_edges"], replace_asset=True, **extra)
    elif operation == "decimate":
        chunk.decimateModel(face_count=step["face_count"], replace_asset=True, **extra)
    elif operation in ("remove_low_confidence", "remove_box", "crop_box"):
        remove_faces(model, step)
    else:
        raise ValueError(f"Неизвестная операция {operation}.")


def remove_faces(model, step):
    """Select faces by vertex confidence or by a box (chunk coordinates) and delete them."""
    vertices = model.vertices
    if step["operation"] == "remove_low_confidence":
        confidence = [float(vertex.confidence) for vertex in vertices]
        top = max(confidence) if confidence else 0
        if top <= 0:
            raise ValueError("У модели нет уверенности вершин (постройте с vertex_confidence).")
        limit = top * step["max_percent"] / 100

        def bad(face):
            return min(confidence[index] for index in face.vertices) < limit
    else:
        low, high = step["min"], step["max"]
        coords = [vertex.coord for vertex in vertices]

        def inside(face):
            points = [coords[index] for index in face.vertices]
            center = [sum(point[axis] for point in points) / 3 for axis in range(3)]
            return all(low[axis] <= center[axis] <= high[axis] for axis in range(3))

        def bad(face):
            return inside(face) if step["operation"] == "remove_box" else not inside(face)
    selected = 0
    for face in model.faces:
        face.selected = bad(face)
        selected += face.selected
    if selected == len(model.faces):
        raise ValueError(f"{step['operation']}: под удаление попали все грани — проверьте порог или рамку.")
    if selected:
        model.removeSelection()
    return selected
