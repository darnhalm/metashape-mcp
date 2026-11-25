"""Pure format mapping and option routing for tiled-model exports."""


TILED_MODEL_FORMATS = {
    "cesium": ("TiledModelFormatCesium", "tiled-model.3tz"),
    "slpk": ("TiledModelFormatSLPK", "tiled-model.slpk"),
    "osgb": ("TiledModelFormatOSGB", "tiled-model.osgb"),
}


def tiled_model_export_spec(format_name, tileset_version="1.0", model_compression=True,
                            use_tileset_transform=True, screen_space_error=16.0,
                            folder_depth=5):
    if format_name not in TILED_MODEL_FORMATS:
        raise ValueError("format: cesium, slpk или osgb.")
    if tileset_version not in ("1.0", "1.1"):
        raise ValueError("tileset_version: 1.0 или 1.1.")
    if not 0 < float(screen_space_error) <= 1024:
        raise ValueError("screen_space_error должен быть больше 0 и не больше 1024.")
    if isinstance(folder_depth, bool) or not isinstance(folder_depth, int) or not 0 <= folder_depth <= 32:
        raise ValueError("folder_depth должен быть целым числом от 0 до 32.")
    constant, filename = TILED_MODEL_FORMATS[format_name]
    arguments = {}
    if format_name == "cesium":
        arguments = {
            "model_compression": bool(model_compression),
            "tileset_version": tileset_version,
            "use_tileset_transform": bool(use_tileset_transform),
            "screen_space_error": float(screen_space_error),
            "folder_depth": folder_depth,
        }
    return {
        "format": format_name,
        "constant": constant,
        "filename": filename,
        "arguments": arguments,
        "cesium_options_applied": format_name == "cesium",
    }
