"""Start the Qt desktop, preferring the checkout's installed desktop environment."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from .paths import APP_ROOT


def run() -> None:
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    if importlib.util.find_spec("PySide6") is None:
        executable = APP_ROOT / ".venv" / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
        if executable.is_file() and executable.resolve() != Path(sys.executable).resolve():
            os.execv(str(executable), [str(executable), "-m", "redump_engine.desktop"])
        message = 'The Qt desktop is not installed. Run: python -m pip install -e ".[desktop]"'
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Install Redump Tools desktop", 0x10)
        raise SystemExit(message)
    from .qt_app import run as run_qt
    run_qt()


if __name__ == "__main__":
    run()
