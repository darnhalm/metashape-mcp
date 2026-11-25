"""Path scoping shared by the Metashape host and tests."""

from pathlib import Path


def normalize_roots(values):
    roots = []
    for value in values or []:
        path = Path(value).expanduser()
        if not path.is_absolute():
            continue
        resolved = path.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def resolve_scoped(value, roots):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Путь должен быть абсолютным.")
    resolved = path.resolve()
    allowed = normalize_roots(roots)
    if not allowed:
        raise ValueError("В настройках Metashape MCP не выбраны разрешённые рабочие папки.")
    for root in allowed:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(f"Путь находится вне разрешённых рабочих папок: {resolved}")


def describe_roots(values):
    return [{"path": str(path), "exists": path.is_dir()} for path in normalize_roots(values)]
