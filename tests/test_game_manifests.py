from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_hardening import FakeChdman

from redump_engine.cuesheets import CueRepository
from redump_engine.game_manifests import manifest_destination, merge_game_manifest
from redump_engine.library import manifest_for, scan_library, selected_paths
from redump_engine.metadata import Manifest, ManifestDisc, load_manifest, save_manifest
from redump_engine.workflow_convert import ConvertWorkflow
from redump_engine.workflow_extract import ExtractWorkflow


def disc_manifest(title: str, number: int, kind: str = "iso") -> Manifest:
    return Manifest(1, "iso" if kind == "iso" else "bin-cue", title,
                    [ManifestDisc(number, [{"name": f"{title}.{kind}", "kind": kind, "size": 1}])])


class GameManifestTests(unittest.TestCase):
    def test_same_payload_merge_preserves_aliases_without_mutating_the_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "manifest.json"
            payload = {"name": "old.iso", "kind": "iso", "size": 1000, "sha1": "a" * 40, "crc32": "12345678"}
            save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [payload], ["backup.chd", "old.iso"])]))
            incoming = Manifest(1, "iso", "Game", [ManifestDisc(1, [{**payload, "name": "renamed.iso"}], ["Game.chd", "renamed.iso"])])
            before = copy.deepcopy(incoming)
            merge_game_manifest(metadata, incoming)
            restored = load_manifest(metadata)
            assert restored is not None
            self.assertEqual(["backup.chd", "old.iso", "Game.chd", "renamed.iso"], restored.disc_entries[0].source_files)
            self.assertEqual(before, incoming)

    def test_different_or_incomplete_payloads_do_not_inherit_old_aliases(self):
        payload = {"name": "old.iso", "kind": "iso", "size": 1000, "sha1": "a" * 40, "crc32": "12345678"}
        for change in ({"sha1": "b" * 40}, {"size": 999}, {"crc32": "87654321"}, {"sha1": "", "crc32": ""}, {"kind": "bin"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                metadata = Path(temporary) / "manifest.json"
                old = {**payload, **change} if change.get("sha1") == "" else payload
                save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [old], ["old.chd"])]))
                merge_game_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [{**payload, **change}], ["new.chd"])]))
                restored = load_manifest(metadata)
                assert restored is not None
                self.assertEqual(["new.chd"], restored.disc_entries[0].source_files)
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [payload, payload], ["two-tracks.chd"])]))
            merge_game_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [payload], ["one-track.chd"])]))
            restored = load_manifest(metadata)
            assert restored is not None
            self.assertEqual(["one-track.chd"], restored.disc_entries[0].source_files)

    def test_dedicated_game_folder_has_one_manifest_for_all_discs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Game (USA)"
            folder.mkdir()
            paths = [folder / f"Game (USA) (Disc {number}).iso" for number in (1, 2)]
            for path in paths:
                path.write_bytes(path.stem.encode())
            workflow = ConvertWorkflow(FakeChdman(), None, keep_sources=True, rename_folder=False)
            for path in paths:
                workflow.process_game_dir(folder, selected_files=[path])
            self.assertEqual([folder / "manifest.json"], list(folder.glob("*.json")))
            manifest = load_manifest(folder / "manifest.json")
            assert manifest is not None
            self.assertEqual("Game (USA)", manifest.title)
            self.assertEqual([1, 2], [disc.disc for disc in manifest.disc_entries])
            self.assertEqual([folder / "manifest.json"], selected_paths(scan_library(folder), "verify"))

    def test_mixed_folder_gets_one_named_manifest_per_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            paths = [folder / name for name in ("Game (Disc 1).iso", "Game (Disc 2).iso", "Other.iso")]
            workflow = ConvertWorkflow(FakeChdman(), None, keep_sources=True, rename_folder=False)
            for path in paths:
                path.write_bytes(path.stem.encode())
            for path in paths:
                workflow.process_game_dir(folder, selected_files=[path])
            self.assertEqual({"Game.manifest.json", "Other.manifest.json"}, {path.name for path in folder.glob("*.json")})
            self.assertEqual(folder / "Game.manifest.json", manifest_for(paths[1]))
            manifest = load_manifest(folder / "Game.manifest.json")
            assert manifest is not None
            self.assertEqual([1, 2], [disc.disc for disc in manifest.disc_entries])

    def test_extracting_one_disc_preserves_other_disc_metadata_and_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Game"
            folder.mkdir()
            for number in (1, 2):
                (folder / f"Game (Disc {number}).chd").write_bytes(str(number).encode())
            manifest = Manifest(1, "iso", "Game", [disc_manifest(f"Game (Disc {number})", number).disc_entries[0] for number in (1, 2)])
            save_manifest(folder / "manifest.json", manifest)
            playlist = folder / "Game.m3u"
            playlist.write_text("Game (Disc 1).chd\nGame (Disc 2).chd\n", encoding="utf-8")
            workflow = ExtractWorkflow(FakeChdman(), None, CueRepository(folder / "cues"), keep_sources=True, rename_folder=False)
            chosen = folder / "Game (Disc 2).chd"
            workflow.process_game_dir(folder, selected_file=chosen, input_manifest=manifest_for(chosen))
            updated = load_manifest(folder / "manifest.json")
            assert updated is not None
            self.assertEqual([1, 2], [disc.disc for disc in updated.disc_entries])
            self.assertEqual(manifest.disc_entries[0], updated.disc_entries[0])
            self.assertFalse((folder / "Game (Disc 1).iso").exists())
            self.assertEqual("Game (Disc 1).chd\nGame (Disc 2).chd\n", playlist.read_text(encoding="utf-8"))

    def test_legacy_per_disc_manifests_are_consolidated_without_losing_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old = folder / "Game (Disc 1).manifest.json"
            save_manifest(old, disc_manifest("Game (Disc 1)", 1))
            other = folder / "Other.manifest.json"
            save_manifest(other, disc_manifest("Other", 1))
            target = folder / "Game.manifest.json"
            merge_game_manifest(target, disc_manifest("Game (Disc 2)", 2))
            manifest = load_manifest(target)
            assert manifest is not None
            self.assertEqual("Game", manifest.title)
            self.assertEqual([1, 2], [disc.disc for disc in manifest.disc_entries])
            self.assertFalse(old.exists())
            self.assertTrue(other.exists())

    def test_unrelated_or_unreadable_generic_manifest_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Game"
            folder.mkdir()
            source = folder / "Game.iso"
            source.touch()
            generic = folder / "manifest.json"
            for contents in ("broken JSON", '{"schema_version": 1, "title": "Other", "set_type": "iso", "discs": []}'):
                generic.write_text(contents, encoding="utf-8")
                target = manifest_destination(source)
                self.assertEqual(folder / "Game.manifest.json", target)
                merge_game_manifest(target, disc_manifest("Game", 1))
                self.assertEqual(contents, generic.read_text(encoding="utf-8"))

    def test_failed_save_does_not_remove_legacy_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            old = folder / "Game (Disc 1).manifest.json"
            save_manifest(old, disc_manifest("Game (Disc 1)", 1))
            with patch("redump_engine.game_manifests.save_manifest", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    merge_game_manifest(folder / "Game.manifest.json", disc_manifest("Game (Disc 2)", 2))
            self.assertTrue(old.exists())
            self.assertFalse((folder / "Game.manifest.json").exists())

    def test_canonical_manifest_cannot_hide_conflicting_sidecar_payloads(self):
        for name in ("manifest.json", "Game.manifest.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                canonical, sidecar = folder / name, folder / "Game (Disc 1).manifest.json"
                payload = {"name": "Game (Disc 1).iso", "kind": "iso", "size": 100, "sha1": "a" * 40, "crc32": "12345678"}
                save_manifest(canonical, Manifest(1, "iso", "Game", [ManifestDisc(1, [payload], ["canonical.chd"])]))
                save_manifest(sidecar, Manifest(1, "iso", "Game", [ManifestDisc(1, [{**payload, "sha1": "b" * 40}], ["backup.chd"])]))
                before = {path: path.read_bytes() for path in folder.iterdir()}
                with self.assertRaisesRegex(ValueError, "Conflicting manifests for disc 1"):
                    merge_game_manifest(canonical, disc_manifest("Game", 2))
                self.assertEqual(before, {path: path.read_bytes() for path in folder.iterdir()})

    def test_compatible_canonical_and_sidecar_aliases_survive_consolidation(self):
        for name in ("manifest.json", "Game.manifest.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                canonical, sidecar = folder / name, folder / "Game (Disc 1).manifest.json"
                payload = {"name": "Canonical.iso", "kind": "iso", "size": 100, "sha1": "a" * 40, "crc32": "12345678"}
                save_manifest(canonical, Manifest(1, "iso", "Game", [ManifestDisc(1, [payload], ["canonical.chd"])]))
                save_manifest(sidecar, Manifest(1, "iso", "Game", [ManifestDisc(1, [{**payload, "name": "Old.iso"}], ["backup.chd"])]))
                backup = folder / "backup.chd"
                backup.write_bytes(b"alternate image")
                incoming = disc_manifest("Game", 2)
                before = copy.deepcopy(incoming)
                merge_game_manifest(canonical, incoming)
                result = load_manifest(canonical)
                assert result is not None
                first = result.disc_entry_for(1)
                assert first is not None
                self.assertEqual(["canonical.chd", "backup.chd"], first.source_files)
                self.assertEqual([payload], first.payload_files)
                self.assertFalse(sidecar.exists())
                self.assertEqual(canonical, manifest_for(backup))
                self.assertEqual(before, incoming)
                self.assertEqual(b"alternate image", backup.read_bytes())

    def test_compatible_sidecars_merge_aliases_and_failed_save_keeps_all_records(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                canonical = folder / "manifest.json"
                payload = {"name": "Game.iso", "kind": "iso", "size": 100, "sha1": "a" * 40, "crc32": "12345678"}
                sidecars = [folder / "Game (Disc 1).manifest.json", folder / "Game (Disc 01).manifest.json"]
                for number, path in enumerate(sidecars):
                    save_manifest(path, Manifest(1, "iso", "Game", [ManifestDisc(1, [payload], [f"copy{number}.chd"])]))
                before = {path: path.read_bytes() for path in folder.iterdir()}
                if fail:
                    with patch("redump_engine.game_manifests.save_manifest", side_effect=OSError("publication failed")):
                        with self.assertRaisesRegex(OSError, "publication failed"):
                            merge_game_manifest(canonical, disc_manifest("Game", 2))
                    self.assertEqual(before, {path: path.read_bytes() for path in folder.iterdir()})
                else:
                    merge_game_manifest(canonical, disc_manifest("Game", 2))
                    result = load_manifest(canonical)
                    assert result is not None
                    self.assertEqual({"copy0.chd", "copy1.chd"}, set(result.disc_entries[0].source_files))
                    self.assertTrue(all(not path.exists() for path in sidecars))

    def test_conflicting_legacy_disc_records_require_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            one, duplicate = folder / "Game (Disc 1).manifest.json", folder / "Game (Disc 01).manifest.json"
            save_manifest(one, disc_manifest("Game (Disc 1)", 1))
            save_manifest(duplicate, disc_manifest("Game (Disc 01)", 1))
            with self.assertRaisesRegex(ValueError, "Conflicting manifests"):
                merge_game_manifest(folder / "Game.manifest.json", disc_manifest("Game (Disc 2)", 2))
            self.assertTrue(one.exists())
            self.assertTrue(duplicate.exists())
