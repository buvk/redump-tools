"""Persistent desktop preferences and recent-folder history."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .json_storage import read_object, write_object
from .paths import APP_ROOT, CUESHEET_ROOT, DAT_ROOT, SETTINGS_PATH

_PATH_KEYS = ("chdman", "extract_xiso", "dolphin_tool", "dats", "cuesheets")


def _portable_path(value: Any, *, expand: bool) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    path = Path(value).expanduser()
    if expand:
        return str(APP_ROOT / path) if not path.is_absolute() else str(path)
    if path.is_absolute():
        try:
            return path.relative_to(APP_ROOT).as_posix()
        except ValueError:
            pass
    return value


def _settings_paths(data: dict[str, Any], *, expand: bool) -> dict[str, Any]:
    result = dict(data)
    for key in _PATH_KEYS:
        if key in result:
            result[key] = _portable_path(result[key], expand=expand)
    downloads = result.get("reference_downloads")
    if isinstance(downloads, dict):
        result["reference_downloads"] = {_portable_path(key, expand=expand): value for key, value in downloads.items()}
    return result


def load_preferences(settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    data = read_object(settings_path)
    if settings_path.resolve() == SETTINGS_PATH.resolve():
        data.update(dats=str(DAT_ROOT), cuesheets=str(CUESHEET_ROOT))
    return _settings_paths(data, expand=True)


def save_preferences(values: dict[str, Any], settings_path: Path = SETTINGS_PATH) -> None:
    data = read_object(settings_path)
    updates = dict(values)
    data.update(updates)
    data.pop("show_log", None)
    if settings_path.resolve() == SETTINGS_PATH.resolve():
        data.pop("dats", None)
        data.pop("cuesheets", None)
    write_object(settings_path, _settings_paths(data, expand=False))


def load_recent_folders(settings_path: Path = SETTINGS_PATH) -> list[str]:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        values = data.get("recent_folders", [])
        if not isinstance(values, list):
            values = []
        values = [data.get("last_folder"), *values]
        folders: list[str] = []
        for value in values:
            if isinstance(value, str) and value.strip() and Path(value).is_dir():
                resolved = str(Path(value).resolve())
                if resolved not in folders:
                    folders.append(resolved)
        return folders[:8]
    except (OSError, ValueError):
        return []


def load_last_folder(settings_path: Path = SETTINGS_PATH) -> Path:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        value = data.get("last_folder") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            folder = Path(value).expanduser()
            if folder.is_dir():
                return folder.resolve()
    except (OSError, ValueError):
        pass
    return APP_ROOT


def save_last_folder(folder: Path, settings_path: Path = SETTINGS_PATH) -> None:
    if not folder.is_dir():
        return
    recent = [str(folder.resolve()), *load_recent_folders(settings_path)]
    recent = list(dict.fromkeys(recent))[:8]
    save_preferences({"last_folder": str(folder.resolve()), "recent_folders": recent}, settings_path)
