"""Preferences retain bundled locations when the application moves."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.ui_settings import load_preferences, save_preferences


class PortableSettingsTests(unittest.TestCase):
    def test_move_preserves_bundled_paths_and_external_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = root / "old", root / "new"
            old.mkdir()
            settings = old / "settings.json"
            external = str(root / "external" / "DolphinTool.exe")
            with patch("redump_engine.ui_settings.APP_ROOT", old):
                save_preferences({"chdman": str(old / "tools/chdman.exe"),
                                  "dats": str(old / "dats"), "dolphin_tool": external,
                                  "last_folder": str(root / "games"),
                                  "reference_downloads": {str(old / "dats"): "today"}}, settings)
            raw = json.loads(settings.read_text())
            self.assertEqual("tools/chdman.exe", raw["chdman"])
            self.assertEqual(external, raw["dolphin_tool"])
            old.rename(new)
            with patch("redump_engine.ui_settings.APP_ROOT", new):
                loaded = load_preferences(new / "settings.json")
            self.assertEqual(str(new / "tools/chdman.exe"), loaded["chdman"])
            self.assertEqual(str(new / "dats"), loaded["dats"])
            self.assertEqual(external, loaded["dolphin_tool"])
            self.assertEqual({str(new / "dats"): "today"}, loaded["reference_downloads"])
            self.assertEqual(str(root / "games"), loaded["last_folder"])
