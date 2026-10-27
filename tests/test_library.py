from __future__ import annotations

import contextlib
import io
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_hardening import FakeChdman
from test_workflows import _dat_xml

from redump_engine.chd_ops import Chdman
from redump_engine.cli import run
from redump_engine.events import ResultRecord, decode_event
from redump_engine.library import item_for, manifest_for, scan_library, selected_paths
from redump_engine.metadata import load_manifest
from redump_engine.ui_runner import DesktopRunner, build_command


class MixedLibraryTests(unittest.TestCase):
    def test_rehash_requires_all_originals_in_the_manifest(self) -> None:
        from redump_engine.metadata import Manifest, ManifestDisc, save_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chd = root / "Game (Disc 1).chd"
            chd.write_bytes(b"chd")
            metadata = root / "Game.manifest.json"
            save_manifest(metadata, Manifest(1, "bin-cue", "Game", [
                ManifestDisc(1, [{"name": "Game (Disc 1).bin", "kind": "bin"}]),
                ManifestDisc(2, [{"name": "Game (Disc 2).bin", "kind": "bin"}]),
            ]))
            item = item_for(chd)
            self.assertTrue(item.eligible("verify"))
            self.assertTrue(item.eligible("verify", check_chds=True))
            self.assertFalse(item.eligible("rehash"))
            (root / "Game (Disc 1).bin").write_bytes(b"one")
            self.assertEqual([], selected_paths([item], "rehash"))
            (root / "Game (Disc 2).bin").write_bytes(b"two")
            self.assertEqual([metadata], selected_paths([item, item_for(metadata)], "rehash"))
            (root / "Game (Disc 1).bin").unlink()
            self.assertFalse(item.eligible("rehash"))

    def test_verify_selection_requires_a_manifest_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, chd = root / "Missing.iso", root / "Missing.chd"
            raw.write_bytes(b"iso")
            chd.write_bytes(b"chd")
            self.assertEqual([], selected_paths([item_for(raw), item_for(chd)], "verify"))
            self.assertEqual([chd], selected_paths([item_for(chd)], "verify", check_chds=True))
            from redump_engine.metadata import Manifest, ManifestDisc, save_manifest
            recorded = root / "Recorded.iso"
            recorded.write_bytes(b"iso")
            metadata = root / "Recorded.manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Recorded", [ManifestDisc(1, [{"name": recorded.name, "kind": "iso"}])]))
            self.assertEqual([metadata], selected_paths([item_for(raw), item_for(recorded)], "verify"))

    def test_repeated_file_retries_preserve_exact_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("redump_engine.ui_runner.threading.Thread"):
            folder = Path(tmp)
            one = folder / "One.iso"
            one.touch()
            runner = DesktopRunner(queue.Queue())
            command = build_command("convert", folder, chdman=folder / "missing.exe", dats=folder, cues=folder)
            try:
                for _ in range(2):
                    runner.start(command, files=[one])
                    command = runner.last_command
                    selection = Path(command[command.index("--file-list") + 1])
                    self.assertEqual([str(one)], json.loads(selection.read_text(encoding="utf-8")))
                    self.assertNotIn("--game-dir", command)
                    self.assertNotIn("--game-list", command)
                    runner.cleanup()
                    self.assertFalse(selection.exists())
            finally:
                runner.cleanup()
    def run_selection(self, root: Path, operation: str, files: list[Path], *flags: str,
                      tool: Chdman | None = None) -> tuple[int, list[ResultRecord]]:
        selection = root / "selection.json"
        selection.write_text(json.dumps([str(path) for path in files]), encoding="utf-8")
        output = io.StringIO()
        test_tool = tool or (Chdman(Path("missing-chdman"), dry_run=True) if "--dry-run" in flags else FakeChdman())
        with patch("redump_engine.cli.Chdman", return_value=test_tool), contextlib.redirect_stdout(output):
            code = run(["--dats", str(root / "dats"), "--cuesheets", str(root / "cuesheets"), operation, "--root", str(root), "--file-list", str(selection), "--events", *flags])
        records = [event.result for line in output.getvalue().splitlines() if (event := decode_event(line)) is not None and event.result is not None]
        return code, records

    def test_scans_mixed_files_and_groups_cue_tracks_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "DVD.ISO").write_bytes(b"iso")
            (folder / "Other.chd").write_bytes(b"chd")
            (folder / "Disc.CUE").write_text('FILE "Track 01.BIN" BINARY\n TRACK 01 MODE2/2352\n', encoding="utf-8")
            (folder / "Track 01.BIN").write_bytes(b"track")
            (folder / "orphan.bin").write_bytes(b"orphan")
            child = folder / "Game"
            child.mkdir()
            (child / "Nested.iso").touch()
            items = scan_library(folder)
            self.assertEqual(5, len(items))
            cue = next(item for item in items if item.kind == "CUE/BIN")
            self.assertEqual((folder / "Track 01.BIN",), cue.dependencies)
            self.assertEqual(cue.path.stat().st_size + 5, cue.size)
            self.assertEqual(4, len(scan_library(folder, recursive=False)))
            self.assertEqual({"DVD.ISO", "Disc.CUE", "Nested.iso"}, {path.name for path in selected_paths(items, "convert")})
            self.assertEqual([folder / "Other.chd"], selected_paths(items, "extract"))

    def test_missing_or_external_cue_tracks_cannot_be_converted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "library"
            folder.mkdir()
            cue = folder / "Disc.cue"
            cue.write_text('FILE "missing.bin" BINARY\n', encoding="utf-8")
            self.assertFalse(item_for(cue).eligible("convert"))
            (root / "outside.bin").touch()
            cue.write_text('FILE "../outside.bin" BINARY\n', encoding="utf-8")
            self.assertFalse(item_for(cue).eligible("convert"))
            self.assertIn("outside", item_for(cue).problem)

    def test_only_selected_iso_is_converted_and_generic_metadata_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chosen, other = folder / "Chosen.iso", folder / "Other.iso"
            chosen.write_bytes(b"selected")
            other.write_bytes(b"untouched")
            generic = folder / "manifest.json"
            generic.write_text("{}", encoding="utf-8")
            code, records = self.run_selection(folder, "convert", [chosen], "--keep-sources")
            self.assertEqual(0, code)
            self.assertEqual(b"selected", (folder / "Chosen.chd").read_bytes())
            self.assertFalse((folder / "Other.chd").exists())
            self.assertEqual(b"untouched", other.read_bytes())
            self.assertEqual("{}", generic.read_text(encoding="utf-8"))
            self.assertTrue((folder / "Chosen.manifest.json").is_file())
            self.assertEqual(chosen.with_suffix(".chd"), records[0].source)
            self.assertIsNotNone(records[0].reference)

    def test_multiple_unrelated_isos_get_independent_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            paths = [folder / "One.iso", folder / "Two.iso"]
            for path in paths:
                path.write_bytes(path.stem.encode())
            code, records = self.run_selection(folder, "convert", paths, "--keep-sources")
            self.assertEqual(0, code)
            self.assertEqual(2, len(records))
            for path in paths:
                manifest = load_manifest(path.with_suffix(".manifest.json"))
                assert manifest is not None
                self.assertEqual(path.name, manifest.disc_entries[0].payload_files[0]["name"])
                self.assertTrue(path.with_suffix(".chd").is_file())
            self.assertFalse((folder / "manifest.json").exists())

    def test_extracts_only_selected_chd_and_preserves_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            one, two = folder / "One.chd", folder / "Two.chd"
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            playlist = folder / "Collection.m3u"
            playlist.write_text("One.chd\nTwo.chd\n", encoding="utf-8")
            code, _ = self.run_selection(folder, "extract", [one], "--keep-sources")
            self.assertEqual(0, code)
            self.assertEqual(b"one", (folder / "One.iso").read_bytes())
            self.assertFalse((folder / "Two.iso").exists())
            self.assertEqual("One.chd\nTwo.chd\n", playlist.read_text(encoding="utf-8"))
            self.assertTrue((folder / "One.manifest.json").exists())

    def test_preview_does_not_modify_selected_or_unselected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            selected = folder / "One.iso"
            selected.write_bytes(b"original")
            code, records = self.run_selection(folder, "convert", [selected], "--dry-run", "--keep-sources")
            self.assertEqual(0, code)
            self.assertEqual("Planned", records[0].status)
            self.assertEqual(b"original", selected.read_bytes())
            self.assertFalse(selected.with_suffix(".chd").exists())
            self.assertFalse(selected.with_suffix(".manifest.json").exists())

    def test_verifies_sidecar_manifest_without_original_iso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            payload = b"disc image"
            iso = folder / "Game.iso"
            iso.write_bytes(payload)
            code, _ = self.run_selection(folder, "convert", [iso])
            self.assertEqual(0, code)
            self.assertFalse(iso.exists())
            dats = folder / "dats"
            dats.mkdir()
            (dats / "game.dat").write_text(_dat_xml("Game", "Game.iso", payload), encoding="utf-8")
            chd = folder / "Game.chd"
            self.assertEqual(folder / "manifest.json", manifest_for(chd))
            manifest = manifest_for(chd)
            assert manifest is not None
            code, records = self.run_selection(folder, "verify", [manifest])
            self.assertEqual(0, code)
            self.assertEqual("Warning", records[0].status)
            self.assertIn("Manifest matches Redump DAT", records[0].details)
            self.assertIn("Game folder name:", records[0].details)
            self.assertFalse(iso.exists())

    def test_original_recheck_reads_payload_while_manifest_check_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            iso = folder / "Game.iso"
            payload = b"original image"
            iso.write_bytes(payload)
            self.assertEqual(0, self.run_selection(folder, "manifest", [iso])[0])
            dats = folder / "dats"
            dats.mkdir()
            (dats / "game.dat").write_text(_dat_xml("Game", iso.name, payload), encoding="utf-8")
            metadata = manifest_for(iso)
            assert metadata is not None
            code, records = self.run_selection(folder, "verify", [metadata], "--rehash")
            self.assertEqual(0, code)
            self.assertIn("Read and rehashed 1", records[0].details)
            iso.write_bytes(b"corrupted image")
            code, records = self.run_selection(folder, "verify", [metadata])
            self.assertEqual(0, code)
            self.assertEqual("Warning", records[0].status)
            self.assertIn("Manifest matches Redump DAT", records[0].details)
            code, records = self.run_selection(folder, "verify", [metadata], "--rehash")
            self.assertEqual(2, code)
            self.assertIn("checksum mismatch on disk", records[0].details)

    def test_selected_cue_cleanup_preserves_bins_used_by_unselected_cue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Collection"
            folder.mkdir()
            track = folder / "shared.bin"
            track.write_bytes(b"shared")
            one, two = folder / "One.cue", folder / "Two.cue"
            for cue in (one, two):
                cue.write_text('FILE "shared.bin" BINARY\n TRACK 01 MODE2/2352\n', encoding="utf-8")
            tool = FakeChdman()
            with patch.object(tool, "createcd", side_effect=lambda source, output: output.write_bytes(b"chd")):
                code, _ = self.run_selection(folder, "convert", [one], tool=tool)
            self.assertEqual(0, code)
            self.assertFalse(one.exists())
            self.assertTrue(two.exists())
            self.assertEqual(b"shared", track.read_bytes())

    def test_missing_manifest_is_skipped_and_selection_deduplicates_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            iso = folder / "Game.iso"
            iso.write_bytes(b"disc")
            code, records = self.run_selection(folder, "verify", [iso])
            self.assertEqual(0, code)
            self.assertEqual("Skipped", records[0].status)
            self.run_selection(folder, "convert", [iso], "--keep-sources")
            items = scan_library(folder)
            self.assertEqual([folder / "manifest.json"], selected_paths(items, "verify"))


class ManifestFolderIndexTests(unittest.TestCase):
    def test_index_visits_only_the_requested_folder(self):
        from redump_engine.library_metadata import ManifestIndex, manifest_for
        from redump_engine.metadata import Manifest, ManifestDisc, save_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests = {}
            images = []
            for number in range(50):
                folder = root / str(number)
                folder.mkdir()
                image = folder / "Game.iso"
                image.write_bytes(b"image")
                metadata = folder / "manifest.json"
                manifest = Manifest(1, "iso", "Game", [ManifestDisc(1, [{"name": image.name}], [image.name])])
                save_manifest(metadata, manifest)
                manifests[metadata] = manifest
                images.append(image)
            class CountingIndex(ManifestIndex):
                visited = 0
                def in_folder(self, folder):
                    found = super().in_folder(folder)
                    self.visited += len(found)
                    return found
            index = CountingIndex(manifests)
            for image in images:
                self.assertEqual(image.parent / "manifest.json", manifest_for(image, index))
            self.assertEqual(len(images), index.visited)
