"""Shared persistent settings for the Metashape MCP launcher and service UI."""

import json
import os
from pathlib import Path


DEFAULTS = {
    "allow_user_scripts": False,
    "user_scripts_directory": "",
    "approved_script_hashes": {},
    "allowed_roots": [],
    "allow_mcp_reload": False,
}


def load_settings(path):
    path = Path(path)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        loaded = {}
    result = {**DEFAULTS, **loaded}
    if not isinstance(result["approved_script_hashes"], dict):
        result["approved_script_hashes"] = {}
    if not isinstance(result["allowed_roots"], list):
        result["allowed_roots"] = []
    return result


def save_settings(path, settings):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
