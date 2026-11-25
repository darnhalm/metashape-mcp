"""Pure numeric helpers for read-only project diagnostics."""

from __future__ import annotations

import math
import statistics


def numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    finite = sorted(float(value) for value in values if math.isfinite(value))
    if not finite:
        return {"count": 0, "min": None, "max": None, "mean": None,
                "median": None, "rms": None, "p05": None, "p95": None}

    def percentile(fraction: float) -> float:
        position = (len(finite) - 1) * fraction
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return finite[lower]
        weight = position - lower
        return finite[lower] * (1 - weight) + finite[upper] * weight

    return {
        "count": len(finite),
        "min": finite[0],
        "max": finite[-1],
        "mean": statistics.fmean(finite),
        "median": statistics.median(finite),
        "rms": math.sqrt(statistics.fmean(value * value for value in finite)),
        "p05": percentile(0.05),
        "p95": percentile(0.95),
    }


def path_diagnostics(rows: list[dict[str, object]]) -> dict[str, object]:
    """Summarize ordered camera positions without assuming universal gap thresholds."""
    positioned = [row for row in rows if row.get("position") is not None]
    steps = []
    for previous, current in zip(positioned, positioned[1:]):
        a = previous["position"]
        b = current["position"]
        distance = math.sqrt(sum((float(b[i]) - float(a[i])) ** 2 for i in range(3)))
        steps.append({
            "from_key": previous["key"], "from_label": previous["label"],
            "to_key": current["key"], "to_label": current["label"],
            "distance": distance,
        })
    distances = [row["distance"] for row in steps]
    stats = numeric_summary(distances)
    median = stats["median"]
    relative_limit = 3 * median if median is not None and median > 0 else None
    gaps = [row for row in steps if relative_limit is not None and row["distance"] > relative_limit]

    extents = None
    if positioned:
        coordinates = [row["position"] for row in positioned]
        minimum = [min(float(point[i]) for point in coordinates) for i in range(3)]
        maximum = [max(float(point[i]) for point in coordinates) for i in range(3)]
        extents = {"min": minimum, "max": maximum,
                   "size": [maximum[i] - minimum[i] for i in range(3)]}
    return {
        "positioned_camera_count": len(positioned),
        "step_statistics": stats,
        "relative_gap_threshold": relative_limit,
        "relative_gaps": gaps[:100],
        "relative_gap_count": len(gaps),
        "extent": extents,
        "order_note": "Шаги рассчитаны в порядке камер чанка; это не считается временем съёмки без проверки имён/метаданных.",
        "threshold_note": "Разрыв — шаг больше трёх медианных; это относительный индикатор, не порог качества.",
    }


def unaligned_runs(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    runs = []
    start = None
    for index, row in enumerate(rows + [{"aligned": True}]):
        if not row.get("aligned") and start is None:
            start = index
        elif row.get("aligned") and start is not None:
            group = rows[start:index]
            runs.append({"start_index": start, "end_index": index - 1, "count": len(group),
                         "first": group[0]["label"], "last": group[-1]["label"]})
            start = None
    return runs
