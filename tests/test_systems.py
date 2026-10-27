from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from redump_engine.dat_loader import parse_dat_xml as _parse_dat_xml
from redump_engine.game_manifests import merge_game_manifest
from redump_engine.library import item_for
from redump_engine.metadata import Manifest, ManifestDisc, load_manifest, save_manifest
from redump_engine.systems import refresh_manifest_systems, system_from_path


class SystemMetadataTests(unittest.TestCase):
    def test_redump_wii_header_and_library_folder_aliases(self):
        for name in ("Nintendo - Wii", "Nintendo - Nintendo Wii", "Nintendo - Wii (2026)"):
            entries = _parse_dat_xml(f'<datafile><header><name>{name}</name></header><game name="Game"/></datafile>')
            self.assertEqual("wii", entries[0].system)
            self.assertEqual("wii", system_from_path(Path("Library") / name / "Game.rvz"))

    def test_exact_folder_mapping_and_unknowns(self) -> None:
        for folder, system in (("Sony - PlayStation", "psx"), ("Sony - PlayStation 2", "ps2"),
                               ("Microsoft - Xbox", "xbox"), ("Nintendo - GameCube", "gamecube"),
                               ("Nintendo - Nintendo Wii", "wii")):
            self.assertEqual(system, system_from_path(Path("library") / folder / "Game" / "Game.iso"))
        self.assertIsNone(system_from_path(Path("library/Sony - PlayStation Portable/Game.iso")))
        self.assertIsNone(system_from_path(Path("library/Mixed/Game.iso")))

    def test_migration_preserves_original_records_and_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Sony - PlayStation 2" / "Game"
            folder.mkdir(parents=True)
            metadata = folder / "manifest.json"
            data = {"schema_version": 1, "set_type": "iso", "title": "Game", "custom": {"keep": True},
                    "discs": [{"disc": 1, "payload_files": [{"name": "Game.iso", "sha1": "abc", "size": 42}]}]}
            metadata.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual("Planned", refresh_manifest_systems(root, True)[0][1])
            self.assertEqual(data, json.loads(metadata.read_text()))
            self.assertEqual("Updated", refresh_manifest_systems(root)[0][1])
            updated = json.loads(metadata.read_text())
            self.assertEqual("ps2", updated.pop("system"))
            self.assertEqual(data, updated)
            self.assertEqual("Skipped", refresh_manifest_systems(root)[0][1])

    def test_manifest_gates_actions_even_outside_system_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            image = folder / "Game.iso"
            image.write_bytes(b"iso")
            metadata = folder / "Game.manifest.json"
            manifest = Manifest(1, "iso", "Game", [ManifestDisc(1, [{"name": image.name}])], "ps2")
            save_manifest(metadata, manifest)
            item = item_for(image)
            self.assertEqual("ps2", item.system)
            self.assertFalse(item.eligible("xiso"))
            self.assertTrue(item.eligible("convert"))
            manifest.system = "xbox"
            save_manifest(metadata, manifest)
            self.assertTrue(item_for(image).eligible("xiso"))
            self.assertFalse(item_for(image).eligible("convert"))
            manifest.system = "gamecube"
            save_manifest(metadata, manifest)
            self.assertFalse(item_for(image).eligible("convert"))
            self.assertFalse(item_for(image).eligible("xiso"))

    def test_legacy_inference_and_dat_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Sony - PlayStation 2"
            folder.mkdir()
            image = folder / "Game.iso"
            image.touch()
            self.assertEqual("ps2", item_for(image).system)
            metadata = folder / "Game.manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [{"name": image.name}])]))
            loaded = load_manifest(metadata)
            assert loaded is not None
            self.assertEqual("ps2", loaded.system)
        entries = _parse_dat_xml('<datafile><header><name>Sony - PlayStation 2 (2026)</name></header><game name="Game"/></datafile>')
        self.assertEqual("ps2", entries[0].system)

    def test_merge_retains_system_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "Game.manifest.json"
            merge_game_manifest(destination, Manifest(1, "iso", "Game", [ManifestDisc(1, [{"name": "Game.iso"}])], "ps2"))
            merge_game_manifest(destination, Manifest(1, "iso", "Game", [ManifestDisc(2, [{"name": "Game 2.iso"}])]))
            loaded = load_manifest(destination)
            assert loaded is not None
            self.assertEqual("ps2", loaded.system)
            with self.assertRaisesRegex(ValueError, "Conflicting manifest systems"):
                merge_game_manifest(destination, Manifest(1, "iso", "Game", [], "xbox"))

    def test_conflicting_migration_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Sony - PlayStation 2"
            folder.mkdir()
            metadata = folder / "manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [], "xbox"))
            before = metadata.read_bytes()
            self.assertEqual("Failed", refresh_manifest_systems(root)[0][1])
            self.assertEqual(before, metadata.read_bytes())
