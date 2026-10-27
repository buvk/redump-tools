"""Locations shared by source checkouts and installed applications."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
IS_CHECKOUT = (SOURCE_ROOT / "pyproject.toml").is_file() and (SOURCE_ROOT / "redump_engine").is_dir()
USER_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "redump-tools"
APP_ROOT = SOURCE_ROOT if IS_CHECKOUT else USER_DATA_ROOT
SETTINGS_PATH = USER_DATA_ROOT / "settings.json"
ICON_PATH = PACKAGE_ROOT / "assets" / "redump-tools.ico"

DAT_ROOT = USER_DATA_ROOT / "dats"
CUESHEET_ROOT = USER_DATA_ROOT / "cuesheets"
