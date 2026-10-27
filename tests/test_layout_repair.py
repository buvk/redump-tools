from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.file_io import write_metadata
from redump_engine.file_transaction import FileTransaction
from redump_engine.metadata import (
    DatEntry,
    DatRom,
    Manifest,
    ManifestDisc,
    load_dat_index,
    load_manifest,
    payload_files_from_paths,
    save_manifest,
)
from redump_engine.naming_analysis import payload_filename as _rom_name
from redump_engine.workflow_layout import plan_layout


class LayoutRepairTests(unittest.TestCase):
    def test_identical_tracks_use_exact_names_or_matching_track_numbers(self) -> None:
        entry = DatEntry("Game (USA)", [DatRom(f"Game (USA) (Track {number:02}).bin", 123, "abcd", "samehash") for number in (31, 62)])
        for number in (31, 62):
            payload = {"name": f"Game (USA) (Track {number}).bin", "size": 123, "crc32": "abcd", "sha1": "samehash"}
            self.assertEqual(payload["name"], _rom_name(payload, entry))
            payload["name"] = f"Old game name (Track {number}).bin"
            self.assertEqual(f"Game (USA) (Track {number}).bin", _rom_name(payload, entry))
        with self.assertRaisesRegex(ValueError, "Game.*Unidentified.bin.*DAT candidates"):
            _rom_name({"name": "Unidentified.bin", "sha1": "samehash"}, entry)
        with self.assertRaisesRegex(ValueError, "no checksum match"):
            _rom_name({"name": "Game (USA) (Track 31).bin", "sha1": "wronghash"}, entry)

    def fixture(self, root: Path, *, cue: bool = False) -> tuple[Path, Path, str]:
        title = "Game (USA) (En,Fr)"
        folder = root / "Sega - Dreamcast" / "Game (USA)" if cue else root / "Nintendo - GameCube" / "Game (USA)"
        folder.mkdir(parents=True)
        original = folder / "Old.bin" if cue else root / "Original.iso"
        original.write_bytes(b"disc payload")
        payloads = payload_files_from_paths([original], "bin" if cue else "iso")
        image = folder / ("Old.cue" if cue else "Old.rvz")
        image.write_bytes(b'FILE "Old.bin" BINARY\r\n  TRACK 01 MODE1/2352\r\n' if cue else b"compressed bytes")
        metadata = folder / "Old.manifest.json"
        save_manifest(metadata, Manifest(1, "bin-cue" if cue else "iso", title,
                                       [ManifestDisc(1, payloads, [image.name])], "dc" if cue else "gamecube"))
        dats = root / "dats"
        dats.mkdir()
        payload = payloads[0]
        extension = ".bin" if cue else ".iso"
        (dats / "game.dat").write_text(f'<datafile><game name="{title}"><rom name="{title}{extension}" '
                                       f'size="{payload["size"]}" sha1="{payload["sha1"]}" crc="{payload["crc32"]}"/>'
                                       '</game></datafile>', encoding="utf-8")
        return metadata, dats, title

    def test_rvz_repair_preserves_payload_and_unknown_fields_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, dats, title = self.fixture(Path(tmp))
            raw = json.loads(metadata.read_text())
            raw["custom"] = {"preserve": True}
            metadata.write_text(json.dumps(raw), encoding="utf-8")
            index = load_dat_index(dats)
            plan = plan_layout(metadata, index)
            self.assertTrue(metadata.exists())  # Planning is read-only.
            self.assertIn("Old.rvz", plan.description())
            plan.apply()
            target = metadata.parent.with_name(title)
            self.assertEqual(b"compressed bytes", (target / (title + ".rvz")).read_bytes())
            self.assertFalse(metadata.parent.exists())
            result = json.loads((target / "manifest.json").read_text())
            self.assertEqual(raw["custom"], result["custom"])
            self.assertEqual(raw["discs"][0]["payload_files"][0]["sha1"], result["discs"][0]["payload_files"][0]["sha1"])
            self.assertEqual(title + ".iso", result["discs"][0]["payload_files"][0]["name"])
            self.assertFalse(plan_layout(target / "manifest.json", index).changed)

    def test_cue_tracks_local_and_parent_playlists_follow_renames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, dats, title = self.fixture(Path(tmp), cue=True)
            folder = metadata.parent
            (folder / "Old.m3u").write_bytes(b"\xef\xbb\xbf#EXTM3U\r\nOld.cue\r\n")
            parent_playlist = folder.parent / "Favorites.m3u"
            parent_playlist.write_text(folder.name + "/Old.cue\n", encoding="utf-8")
            plan = plan_layout(metadata, load_dat_index(dats))
            plan.apply()
            target = folder.with_name(title)
            self.assertIn((title + '.bin" BINARY\r\n').encode(), (target / (title + ".cue")).read_bytes())
            self.assertEqual(b"disc payload", (target / (title + ".bin")).read_bytes())
            self.assertEqual(b"\xef\xbb\xbf#EXTM3U\r\n" + (title + ".cue\r\n").encode(), (target / "Old.m3u").read_bytes())
            self.assertEqual(title + "/" + title + ".cue\n", parent_playlist.read_text())

    def test_external_cue_references_block_track_renames_without_changing_files(self):
        for same_folder in (False, True):
            for nested in (False, True):
                with self.subTest(same_folder=same_folder, nested=nested), tempfile.TemporaryDirectory() as temporary:
                    metadata, dats, title = self.fixture(Path(temporary), cue=True)
                    folder = metadata.parent
                    if same_folder:
                        renamed = folder.with_name(title)
                        folder.rename(renamed)
                        metadata, folder = renamed / metadata.name, renamed
                    other = folder / "Nested" / "Other.cue" if nested else folder.parent / "Other.cue"
                    other.parent.mkdir(exist_ok=True)
                    relative = os.path.relpath(folder / "Old.bin", other.parent).replace("\\", "/")
                    other.write_text(f'FILE "{relative}" BINARY\n TRACK 01 MODE1/2352\n')
                    before = {path: path.read_bytes() for path in folder.parent.rglob("*") if path.is_file()}
                    with self.assertRaisesRegex(ValueError, "referenced by other CUE files"):
                        plan_layout(metadata, load_dat_index(dats))
                    self.assertEqual(before, {path: path.read_bytes() for path in folder.parent.rglob("*") if path.is_file()})

    def test_multiple_custom_playlists_keep_names_formatting_and_parent_links(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                metadata, dats, title = self.fixture(root, cue=True)
                folder = metadata.parent
                playlists = {
                    "My order.m3u": b"\xef\xbb\xbf# Disc order\r\nOld.cue\r\nOther/Other.cue",
                    "Alternate order.m3u": b"# Alternate\nOther/Other.cue\nOld.cue\n",
                }
                for name, data in playlists.items():
                    (folder / name).write_bytes(data)
                parent = folder.parent / "Favorites.m3u"
                parent.write_text(folder.name + "/My order.m3u\n" + folder.name + "/Alternate order.m3u\n")
                index = load_dat_index(dats)
                before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
                plan = plan_layout(metadata, index)
                if fail:
                    write = write_metadata
                    def fail_publication(path, content, target=plan.manifest, write=write):
                        if path == target:
                            raise OSError("publication failed")
                        return write(path, content)
                    with patch("redump_engine.workflow_layout.write_metadata", side_effect=fail_publication):
                        with self.assertRaisesRegex(OSError, "publication failed"):
                            plan.apply()
                    self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
                else:
                    plan.apply()
                    for name, data in playlists.items():
                        self.assertEqual(data.replace(b"Old.cue", (title + ".cue").encode()), (plan.folder / name).read_bytes())
                    self.assertEqual(title + "/My order.m3u\n" + title + "/Alternate order.m3u\n", parent.read_text())
                    self.assertFalse(folder.exists())
                    self.assertFalse(plan_layout(plan.manifest, index).changed)

    def test_repair_and_rollback_do_not_modify_hard_linked_backup_metadata(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                metadata, dats, title = self.fixture(root, cue=True)
                folder = metadata.parent
                playlist = folder / "Custom.m3u"
                playlist.write_bytes(b"# Selected order\r\nOld.cue\r\n")
                backup_folder = folder.parent / "Backups"
                backup_folder.mkdir()
                backups = []
                for path in (folder / "Old.cue", playlist, metadata):
                    backup = backup_folder / path.name
                    os.link(path, backup)
                    backups.append(backup)
                (backup_folder / "Old.bin").write_bytes(b"disc payload")
                original_backups = {path: path.read_bytes() for path in backups}
                plan = plan_layout(metadata, load_dat_index(dats))
                before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
                if fail:
                    write = write_metadata
                    def fail_manifest(path, content, target=plan.manifest, write=write):
                        if path == target:
                            raise OSError("publication failed")
                        return write(path, content)
                    with patch("redump_engine.workflow_layout.write_metadata", side_effect=fail_manifest):
                        with self.assertRaisesRegex(OSError, "publication failed"):
                            plan.apply()
                    self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
                else:
                    plan.apply()
                    self.assertIn((title + '.bin"').encode(), (plan.folder / (title + ".cue")).read_bytes())
                    self.assertIn((title + ".cue").encode(), (plan.folder / playlist.name).read_bytes())
                    self.assertFalse((plan.folder / (title + ".cue")).samefile(backup_folder / "Old.cue"))
                self.assertEqual(original_backups, {path: path.read_bytes() for path in backups})
                self.assertTrue((backup_folder / "Old.bin").exists())

    def test_collision_and_stale_preview_leave_sources_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, dats, title = self.fixture(Path(tmp))
            index = load_dat_index(dats)
            plan = plan_layout(metadata, index)
            metadata.write_text(metadata.read_text() + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed since planning"):
                plan.apply()
            self.assertTrue((metadata.parent / "Old.rvz").is_file())
            target = metadata.parent.with_name(title)
            target.mkdir()
            (target / (title + ".rvz")).write_bytes(b"keep existing")
            with self.assertRaises(ValueError):
                plan_layout(metadata, index)
            self.assertEqual(b"keep existing", (target / (title + ".rvz")).read_bytes())

    def test_failure_restores_images_tracks_metadata_and_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, dats, title = self.fixture(Path(tmp), cue=True)
            (metadata.parent / "Old.m3u").write_bytes(b"Old.cue\n")
            index = load_dat_index(dats)
            before = {file.relative_to(Path(tmp)): file.read_bytes() for file in Path(tmp).rglob("*") if file.is_file()}
            plan = plan_layout(metadata, index)
            transfer = FileTransaction.transfer
            count = 0

            def fail(transaction: FileTransaction, source: Path, target: Path, *, copy: bool = False, source_guard=None) -> None:
                nonlocal count
                transfer(transaction, source, target, copy=copy, source_guard=source_guard)
                count += 1
                if count == 2:
                    raise OSError("simulated failure")

            with patch.object(FileTransaction, "transfer", fail), self.assertRaisesRegex(OSError, "simulated failure"):
                plan.apply()
            after = {file.relative_to(Path(tmp)): file.read_bytes() for file in Path(tmp).rglob("*") if file.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(metadata.parent.with_name(title).exists())

    def test_same_game_sidecars_consolidate_without_losing_disc_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metadata, dats, title = self.fixture(Path(tmp))
            other = metadata.parent / "Other.manifest.json"
            other.write_bytes(metadata.read_bytes())
            plan = plan_layout(metadata, load_dat_index(dats))
            plan.apply()
            result = load_manifest(plan.manifest)
            assert result is not None
            self.assertEqual(1, len(result.disc_entries))
            self.assertEqual(title, result.title)
            self.assertFalse(other.exists())
