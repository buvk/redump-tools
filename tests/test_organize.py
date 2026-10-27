from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import Chdman
from redump_engine.file_io import write_metadata
from redump_engine.library import item_for, scan_library, selected_paths
from redump_engine.metadata import Manifest, ManifestDisc, load_manifest, save_manifest
from redump_engine.ui_runner import build_command
from redump_engine.workflow_convert import ConvertWorkflow
from redump_engine.workflow_organize import organize_destination, organize_file


class OrganizeTests(unittest.TestCase):
    def test_existing_game_folder_with_different_dat_name_is_not_nested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            system = Path(tmp) / "Sega - Dreamcast"
            game = system / "Crazy Taxi (USA)"
            game.mkdir(parents=True)
            image = game / "Crazy Taxi (USA) (En,Ja).chd"
            image.write_bytes(b"chd")
            self.assertEqual(game, organize_destination(image))
            self.assertFalse(item_for(image).eligible("organize"))
            self.assertEqual("Skipped", organize_file(image)[0])
            self.assertEqual([image], list(game.iterdir()))
            # A system folder and a folder containing unrelated games still
            # need the organize action, even when there is a manifest.
            loose = system / "Other.chd"
            loose.write_bytes(b"other")
            self.assertTrue(item_for(loose).eligible("organize"))
            (game / "Other.chd").write_bytes(b"other")
            self.assertTrue(item_for(image).eligible("organize"))

    def test_manifest_identifies_custom_game_folder_and_multiple_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "Custom folder"
            game.mkdir()
            image = game / "Canonical Game.chd"
            source = game / "Original Name.iso"
            image.write_bytes(b"chd")
            source.write_bytes(b"iso")
            save_manifest(game / "manifest.json", Manifest(1, "iso", "Canonical Game", [
                ManifestDisc(1, [{"name": source.name}], source_files=[source.name, image.name]),
            ]))
            for path in (image, source):
                self.assertEqual(game, organize_destination(path))
                self.assertTrue(item_for(path).eligible("organize"))  # Naming repair remains available in a game folder.
                self.assertEqual("Skipped", organize_file(path, True)[0])

    def test_xiso_label_selection_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Burnout (USA).XISO.ISO"
            image.write_bytes(b"xbox")
            item = item_for(image)
            self.assertEqual("XISO", item.kind)
            self.assertFalse(item.eligible("convert"))
            self.assertFalse(item.eligible("manifest"))
            workflow = ConvertWorkflow(Chdman(root / "missing.exe"), None, False, False, True)
            self.assertEqual([], workflow._discover_sources(root))
            self.assertEqual([image], selected_paths(scan_library(root), "organize"))
            self.assertEqual(root / "Burnout (USA)", organize_destination(image))
            self.assertEqual("Completed", organize_file(image)[0])
            target = root / "Burnout (USA)" / image.name
            self.assertEqual(b"xbox", target.read_bytes())
            self.assertEqual("Skipped", organize_file(target)[0])
            self.assertFalse(item_for(target).eligible("organize"))
            loose = root / "Other.iso"
            loose.write_bytes(b"iso")
            self.assertEqual([loose], selected_paths([item_for(target), item_for(loose)], "organize"))

    def test_preview_and_collision_leave_sources_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Game.iso"
            image.write_bytes(b"source")
            self.assertEqual("Planned", organize_file(image, True)[0])
            self.assertFalse((root / "Game").exists())
            (root / "Game").mkdir()
            (root / "Game" / image.name).write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                organize_file(image)
            self.assertEqual(b"source", image.read_bytes())
            self.assertEqual(b"existing", (root / "Game" / image.name).read_bytes())
            command = build_command("organize", root, chdman=root, dats=root, cues=root, preview=True)
            self.assertIn("--dry-run", command)

    def test_cue_tracks_shared_tracks_and_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cue = root / "Game.cue"
            cue.write_text('FILE "track.bin" BINARY\n TRACK 01 MODE1/2352\n', encoding="utf-8")
            other = root / "Other.cue"
            other.write_bytes(cue.read_bytes())
            track = root / "track.bin"
            track.write_bytes(b"track")
            playlist = root / "games.m3u"
            playlist.write_text("# Games\nGame.cue\nOther.cue\n", encoding="utf-8")
            organize_file(cue)
            self.assertEqual(b"track", track.read_bytes())
            self.assertEqual(b"track", (root / "Game" / "track.bin").read_bytes())
            self.assertIn("Game/Game.cue\n", playlist.read_text())
            self.assertTrue(other.exists())

    def test_multidisc_manifest_follows_selected_discs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two = root / "Game (Disc 1).iso", root / "Game (Disc 2).chd"
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            metadata = root / "Game.manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [
                ManifestDisc(1, [{"name": one.name, "kind": "iso", "size": 3}]),
                ManifestDisc(2, [{"name": "Game (Disc 2).iso", "kind": "iso", "size": 10}]),
            ]))
            organize_file(one)
            self.assertTrue(metadata.exists())
            retained = load_manifest(metadata)
            assert retained is not None
            self.assertEqual([2], [entry.disc for entry in retained.disc_entries])
            target = root / "Game" / "manifest.json"
            manifest = load_manifest(target)
            assert manifest is not None
            self.assertEqual([1], [entry.disc for entry in manifest.disc_entries])
            organize_file(two)
            self.assertFalse(metadata.exists())
            manifest = load_manifest(target)
            assert manifest is not None
            self.assertEqual([1, 2], [entry.disc for entry in manifest.disc_entries])

    def test_metadata_write_failure_rolls_back_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "Game.iso"
            image.write_bytes(b"source")
            metadata = root / "Game.manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [{"name": image.name}])]))
            original = metadata.read_bytes()
            real_write = write_metadata
            failed = False

            def fail_once(path: Path, content: bytes) -> int:
                nonlocal failed
                if path.name == "manifest.json" and not failed:
                    failed = True
                    raise OSError("test write failure")
                return real_write(path, content)

            with patch("redump_engine.workflow_layout.write_metadata", side_effect=fail_once), self.assertRaises(OSError):
                organize_file(image)
            self.assertEqual(b"source", image.read_bytes())
            self.assertEqual(original, metadata.read_bytes())
            self.assertFalse((root / "Game").exists())
