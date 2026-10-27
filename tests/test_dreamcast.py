from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from redump_engine.chd_ops import ChdInfo, Chdman
from redump_engine.cuesheets import CueRepository
from redump_engine.library import item_for, manifest_for
from redump_engine.metadata import load_manifest
from redump_engine.systems import system_from_path
from redump_engine.updater import selected_assets
from redump_engine.workflow_extract import ExtractWorkflow
from redump_engine.workflow_manifest import ManifestWorkflow


class DreamcastTests(unittest.TestCase):
    def tool(self) -> MagicMock:
        tool = MagicMock(spec=Chdman)
        tool.info.return_value = ChdInfo(1200 * 1024 * 1024, 1, True, False, "")

        def split(source: Path, cue: Path) -> str:
            one, two = cue.with_name(cue.stem + " (Track 1).bin"), cue.with_name(cue.stem + " (Track 2).bin")
            one.write_bytes(b"data track")
            two.write_bytes(b"audio track")
            cue.write_text(f'FILE "{one.name}" BINARY\n TRACK 01 MODE1/2352\n'
                           f'FILE "{two.name}" BINARY\n TRACK 02 AUDIO\n', encoding="utf-8")
            return ""

        tool.splitbin.side_effect = split
        tool.extractdvd.side_effect = AssertionError("Dreamcast must not use DVD extraction")
        return tool

    def test_folder_system_actions_and_asset_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Sega - Dreamcast" / "Game"
            folder.mkdir(parents=True)
            cue = folder / "Game.cue"
            cue.write_text('FILE "Game.bin" BINARY\n TRACK 01 MODE1/2352\n', encoding="utf-8")
            (folder / "Game.bin").write_bytes(b"track")
            self.assertEqual("dc", system_from_path(cue))
            self.assertTrue(item_for(cue).eligible("convert"))
            self.assertFalse(item_for(cue).eligible("xiso"))
            iso = folder / "Game.iso"
            iso.touch()
            self.assertFalse(item_for(iso).eligible("convert"))
        self.assertEqual({"dat", "cue"}, {asset.kind for asset in selected_assets(["dc"])})

    def test_large_dreamcast_disc_extracts_as_split_bin_cue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "Sega - Dreamcast" / "Game"
            game.mkdir(parents=True)
            chd = game / "Game.chd"
            chd.write_bytes(b"chd")
            tool = self.tool()
            workflow = ExtractWorkflow(tool, None, CueRepository(root), keep_sources=True, rename_folder=False)
            result = workflow.process_game_dir(game)[0]
            self.assertEqual("bin-cue-split", result.decision)
            tool.splitbin.assert_called_once()
            tool.extractdvd.assert_not_called()
            manifest = load_manifest(game / "manifest.json")
            assert manifest is not None
            self.assertEqual("dc", manifest.system)
            self.assertEqual(2, manifest.disc_entries[0].track_count)
            self.assertTrue(chd.exists())

    def test_manifest_creation_temporarily_extracts_tracks_not_dvd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Sega - Dreamcast"
            folder.mkdir()
            chd = folder / "Game.chd"
            chd.write_bytes(b"chd")
            tool = self.tool()
            self.assertEqual("Completed", ManifestWorkflow(tool, None, CueRepository(root)).process_file(chd)[0])
            metadata = manifest_for(chd)
            assert metadata is not None
            manifest = load_manifest(metadata)
            assert manifest is not None
            self.assertEqual("dc", manifest.system)
            self.assertEqual(2, manifest.disc_entries[0].track_count)
            tool.extractdvd.assert_not_called()
            self.assertTrue(chd.exists())

    def test_gdrom_track_metadata_is_recognized(self) -> None:
        output = "Logical size: 1,200,000,000 bytes\nMetadata: Tag='CHGT' Length=1\nMetadata: Tag='CHGT' Length=1\n"
        with patch("redump_engine.chd_ops.subprocess.run", return_value=SimpleNamespace(stdout=output, stderr="", returncode=0)):
            info = Chdman(Path("chdman.exe")).info(Path("Game.chd"))
        self.assertTrue(info.has_gd_metadata)
        self.assertTrue(info.has_cd_metadata)
        self.assertEqual(2, info.track_count)
