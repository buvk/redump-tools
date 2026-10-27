"""Validate persisted desktop layout before applying it to desktop widgets."""
from __future__ import annotations

from typing import Any

LIBRARY_FILTERS = ("All discs", "Compressed", "Uncompressed", "Missing manifest")
LIBRARY_COLUMNS = ("name", "kind", "folder", "size", "original", "saved", "manifest", "checked")


def library_layout(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    selected_filter = data.get("filter")
    if selected_filter in ("Compressed (CHD)", "Compressed (RVZ)", "Compressed (XISO)"):
        selected_filter = "Compressed"
    widths = data.get("columns", {})
    widths = widths if isinstance(widths, dict) else {}
    groups = data.get("groups", {})
    groups = groups if isinstance(groups, dict) else {}
    return {
        "root": data.get("root") if isinstance(data.get("root"), str) else "",
        "system": data.get("system") if isinstance(data.get("system"), str) else "All systems",
        "filter": selected_filter if selected_filter in LIBRARY_FILTERS else "All discs",
        "query": data.get("query") if isinstance(data.get("query"), str) else "",
        "recursive": data.get("recursive") is not False,
        "group_discs": data.get("group_discs") is not False,
        "sort_column": data.get("sort_column") if data.get("sort_column") in LIBRARY_COLUMNS else "name",
        "reverse": data.get("reverse") is True,
        "columns": {key: width for key, width in widths.items() if key in LIBRARY_COLUMNS and type(width) is int and 70 <= width <= 10000},
        "sidebar_width": data.get("sidebar_width") if type(data.get("sidebar_width")) is int and 120 <= data["sidebar_width"] <= 1000 else 230,
        "groups": {key: opened for key, opened in groups.items() if isinstance(key, str) and key.startswith("game:") and type(opened) is bool},
    }


def window_layout(value: Any, screen_width: int, screen_height: int) -> tuple[str, bool]:
    data = value if isinstance(value, dict) else {}
    width = data.get("width")
    height = data.get("height")
    width = width if type(width) is int and width >= 940 else 1120
    height = height if type(height) is int and height >= 720 else 850
    return f"{min(width, screen_width)}x{min(height, screen_height)}", data.get("maximized") is True
