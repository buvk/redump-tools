from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_conversion_naming import NamingTool

from redump_engine.cue import bin_files_from_cue
from redump_engine.filenames import safe_name
from redump_engine.game_folders import contains_one_game, is_game_folder
from redump_engine.game_manifests import manifest_destination
from redump_engine.library import item_for
from redump_engine.metadata import Manifest, ManifestDisc, save_manifest
from redump_engine.workflow_convert import ConvertWorkflow
from redump_engine.workflow_organize import organize_file


class SharedFileRulesTests(unittest.TestCase):
    def test_conversion_keeps_tracks_referenced_by_cues_outside_the_game_folder(self):
        for location in ("parent", "sibling", "nested"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                platform = Path(temporary) / "Sony - PlayStation"
                folder = platform / "Game"
                folder.mkdir(parents=True)
                source, track = folder / "Game.cue", folder / "Shared.bin"
                track.write_bytes(b"shared track")
                source.write_text('FILE "Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
                other = platform / "Other.cue" if location == "parent" else (platform / "Sibling" if location == "sibling" else folder / "Nested") / "Other.CUE"
                other.parent.mkdir(exist_ok=True)
                relative = os.path.relpath(track, other.parent).replace("\\", "/")
                other.write_text(f'FILE "{relative}" BINARY\n TRACK 01 MODE1/2352\n')
                other_bytes = other.read_bytes()
                ConvertWorkflow(NamingTool(), None, keep_sources=False, rename_folder=False).process_game_dir(folder, [source])
                self.assertFalse(source.exists())
                self.assertTrue(source.with_suffix(".chd").exists())
                self.assertEqual(b"shared track", track.read_bytes())
                self.assertEqual(other_bytes, other.read_bytes())
                self.assertTrue(all(path.is_file() for path in bin_files_from_cue(other)))
                if location == "parent":
                    self.assertTrue(item_for(other).eligible("convert"))

    def test_all_converted_cues_can_remove_their_exclusively_shared_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Game"
            folder.mkdir()
            track = folder / "Shared.bin"
            track.write_bytes(b"shared track")
            sources = [folder / f"Game (Disc {number}).cue" for number in (1, 2)]
            for source in sources:
                source.write_text('FILE "Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
            ConvertWorkflow(NamingTool(), None, keep_sources=False, rename_folder=False).process_game_dir(folder)
            self.assertFalse(track.exists())
            self.assertTrue(all(not source.exists() and source.with_suffix(".chd").exists() for source in sources))

    def test_unreadable_reference_keeps_original_cue_and_tracks(self):
        with tempfile.TemporaryDirectory() as temporary:
            platform = Path(temporary) / "Sony - PlayStation"
            folder = platform / "Game"
            folder.mkdir(parents=True)
            source, track, other = folder / "Game.cue", folder / "Shared.bin", platform / "Other.cue"
            source.write_text('FILE "Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
            track.write_bytes(b"original track")
            other.write_text('FILE "Game/Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
            def read_reference(path):
                if path == other:
                    raise PermissionError("cannot read other CUE")
                return bin_files_from_cue(path)
            warnings = []
            workflow = ConvertWorkflow(NamingTool(), None, keep_sources=False, rename_folder=False, on_warning=warnings.append)
            with patch("redump_engine.cue.bin_files_from_cue", side_effect=read_reference), contextlib.redirect_stdout(io.StringIO()):
                workflow.process_game_dir(folder, [source])
            self.assertTrue(source.exists())
            self.assertEqual(b"original track", track.read_bytes())
            self.assertTrue(source.with_suffix(".chd").exists())
            self.assertTrue(any("cannot read other CUE" in warning for warning in warnings))

    def test_organizing_a_cue_keeps_tracks_used_by_a_parent_playlist_cue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collection = root / "Collection"
            collection.mkdir()
            source, track, other = collection / "Game.cue", collection / "Shared.bin", root / "Other.cue"
            source.write_text('FILE "Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
            track.write_bytes(b"shared original")
            other.write_text('FILE "Collection/Shared.bin" BINARY\n TRACK 01 MODE1/2352\n')
            self.assertEqual("Completed", organize_file(source)[0])
            self.assertEqual(b"shared original", track.read_bytes())
            self.assertEqual(b"shared original", (collection / "Game" / "Shared.bin").read_bytes())
            self.assertTrue(all(path.is_file() for path in bin_files_from_cue(other)))
            self.assertTrue(all(path.is_file() for path in bin_files_from_cue(collection / "Game" / "Game.cue")))

    def test_windows_components(self) -> None:
        self.assertEqual("Game_ Name", safe_name('Game: Name. '))
        self.assertEqual("_CON", safe_name("CON"))
        self.assertEqual("_lpt1.txt", safe_name("lpt1.txt"))
        for value in ("", ".", "..", "   "):
            with self.assertRaises(ValueError):
                safe_name(value)

    def test_manifest_aliases_are_shared_by_folder_and_manifest_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Sega - Dreamcast" / "Custom title"
            folder.mkdir(parents=True)
            source, compressed = folder / "Original.cue", folder / "DAT Name.chd"
            source.touch()
            compressed.touch()
            save_manifest(folder / "manifest.json", Manifest(1, "bin-cue", "DAT Name", [
                ManifestDisc(1, [{"name": "track.bin"}], source_files=[source.name, compressed.name]),
            ]))
            self.assertTrue(contains_one_game(folder, ["DAT Name"]))
            self.assertTrue(is_game_folder(folder, "DAT Name"))
            self.assertEqual(folder / "manifest.json", manifest_destination(source, "DAT Name"))
            (folder / "Unrelated.iso").touch()
            self.assertFalse(contains_one_game(folder, ["DAT Name"]))
            self.assertFalse(is_game_folder(folder, "DAT Name"))
            self.assertEqual(folder / "DAT Name.manifest.json", manifest_destination(source, "DAT Name"))

    def test_organization_preserves_playlist_bytes_and_updates_parent_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Collection"
            folder.mkdir()
            image = folder / "Game.iso"
            image.write_bytes(b"image")
            local = folder / "games.m3u"
            local.write_bytes(b"\xef\xbb\xbf# Comment\r\nGame.iso\r\nOther.iso")
            parent = root / "all.m3u"
            parent.write_bytes(b"# Collection\r\nCollection/Game.iso\r\n")
            before = local.read_bytes(), parent.read_bytes()
            self.assertEqual("Planned", organize_file(image, True)[0])
            self.assertEqual(before, (local.read_bytes(), parent.read_bytes()))
            self.assertEqual("Completed", organize_file(image)[0])
            self.assertEqual(b"\xef\xbb\xbf# Comment\r\nGame/Game.iso\r\nOther.iso", local.read_bytes())
            self.assertEqual(b"# Collection\r\nCollection/Game/Game.iso\r\n", parent.read_bytes())
