"""Pure helpers for labelling Metashape measurements without inventing units."""

import math


def measurement_basis(scale, coordinate_system, elevation_available, dem_required=True):
    scaled = (not isinstance(scale, bool) and isinstance(scale, (int, float))
              and math.isfinite(float(scale)) and float(scale) > 0)
    if scaled:
        units = {"length": "meters", "area": "square_meters", "volume": "cubic_meters"}
        warnings = []
    else:
        units = {"length": "chunk_units", "area": "square_chunk_units",
                 "volume": "cubic_chunk_units"}
        warnings = ["У чанка нет подтверждённого масштаба: значения условные, не метры."]
    if dem_required and not elevation_available:
        warnings.append("Активной ЦМР нет: измерения on_dem и объём по фигуре недоступны.")
    return {"scaled": scaled, "chunk_scale": float(scale) if scaled else None,
            "coordinate_system": coordinate_system, "units": units,
            "elevation_available": bool(elevation_available), "warnings": warnings}
