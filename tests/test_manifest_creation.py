from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_hardening import FakeChdman
from test_workflows import _dat_xml

from redump_engine.chd_ops import ChdInfo, ChdmanError
from redump_engine.cli import run
from redump_engine.cuesheets import CueRepository
from redump_engine.dat_models import DatEntry, DatIndex, DatRom
from redump_engine.dolphin_ops import DolphinTool
from redump_engine.library import item_for, manifest_for, scan_library, selected_paths
from redump_engine.metadata import Manifest, ManifestDisc, compute_hashes, load_dat_index, load_manifest, save_manifest
from redump_engine.workflow_manifest import ManifestWorkflow
from redump_engine.workflow_verify import VerifyWorkflow


class ManifestCreationTests(unittest.TestCase):
    def test_changed_compressed_source_rolls_back_manifest_creation(self):
        for extension in (".chd", ".rvz"):
            for existing in (False, True):
                with self.subTest(extension=extension, existing=existing), tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary) / "Game"
                    folder.mkdir()
                    source = folder / ("Game" + extension)
                    source.write_bytes(b"original image")
                    metadata = folder / "manifest.json"
                    if existing:
                        save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, [], [source.name])]))
                    before = {path: path.read_bytes() for path in folder.iterdir() if path.suffix == ".json"}
                    tool = FakeChdman()
                    dolphin = DolphinTool(folder / "unused")
                    def change_source(input_image, output_image, source=source, **options):
                        output_image.write_bytes(b"original extracted payload")
                        stat = source.stat()
                        source.write_bytes(b"modified image")
                        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                        return ""
                    workflow = ManifestWorkflow(tool, None, CueRepository(folder), dolphin=dolphin)
                    with patch.object(tool, "extractdvd", side_effect=change_source), patch.object(dolphin, "convert", side_effect=change_source):
                        with self.assertRaisesRegex(ValueError, "Source changed"):
                            workflow.process_file(source)
                    self.assertEqual(b"modified image", source.read_bytes())
                    self.assertEqual(before, {path: path.read_bytes() for path in folder.iterdir() if path.suffix == ".json"})
                    self.assertEqual({source, *before}, set(folder.iterdir()))

    def test_iso_and_cue_track_changes_during_hashing_roll_back_metadata(self):
        from redump_engine.workflow_manifest import payload_files_from_paths as hash_payloads
        for cue_mode in (False, True):
            with self.subTest(cue_mode=cue_mode), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary) / "Game"
                folder.mkdir()
                payload = folder / ("Track.bin" if cue_mode else "Game.iso")
                payload.write_bytes(b"original image")
                source = folder / "Game.cue" if cue_mode else payload
                if cue_mode:
                    source.write_text('FILE "Track.bin" BINARY\n TRACK 01 AUDIO\n INDEX 01 00:00:00\n')
                def change_after_hash(*args, payload=payload, **kwargs):
                    records = hash_payloads(*args, **kwargs)
                    stat = payload.stat()
                    payload.write_bytes(b"modified image")
                    os.utime(payload, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    return records
                workflow = ManifestWorkflow(FakeChdman(), None, CueRepository(folder))
                with patch("redump_engine.workflow_manifest.payload_files_from_paths", side_effect=change_after_hash):
                    with self.assertRaisesRegex(ValueError, "Source changed"):
                        workflow.process_file(source)
                self.assertEqual(b"modified image", payload.read_bytes())
                self.assertFalse(list(folder.glob("*.json")))
                self.assertEqual({source, payload}, set(folder.iterdir()))

    def test_manifest_write_failure_restores_consolidated_sidecars(self):
        from redump_engine.game_manifests import merge_game_manifest
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Game"
            folder.mkdir()
            source = folder / "Game.iso"
            source.write_bytes(b"original image")
            sidecar = folder / "Game.manifest.json"
            save_manifest(sidecar, Manifest(1, "iso", "Game", [ManifestDisc(1, [], [source.name])]))
            before = sidecar.read_bytes()
            watched = []
            workflow = ManifestWorkflow(FakeChdman(), None, CueRepository(folder), on_write=watched.append)
            def fail_after_merge(*args):
                merge_game_manifest(*args)
                raise OSError("manifest publication failed")
            with patch("redump_engine.workflow_manifest.merge_game_manifest", side_effect=fail_after_merge):
                with self.assertRaisesRegex(OSError, "manifest publication failed"):
                    workflow.process_file(source)
            self.assertEqual([folder / "manifest.json"], watched)
            self.assertEqual(before, sidecar.read_bytes())
            self.assertEqual({source, sidecar}, set(folder.iterdir()))

    def test_created_manifest_claims_only_complete_system_scoped_dat_matches(self):
        scenarios = (
            ("missing track", [b"first"], [b"first", b"second"], "psx", 0, False),
            ("missing duplicate", [b"first"], [b"first", b"first"], "psx", 0, False),
            ("wrong size", [b"first"], [b"first"], "psx", 1, False),
            ("wrong system", [b"first"], [b"first"], "dc", 0, False),
            ("complete", [b"first", b"second"], [b"first", b"second"], "psx", 0, True),
            ("complete duplicates", [b"first", b"first"], [b"first", b"first"], "psx", 0, True),
        )
        for name, inputs, expected, system, size_delta, matched in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary) / "Sony - PlayStation" / "Input"
                folder.mkdir(parents=True)
                cue = folder / "Input.cue"
                lines = []
                for number, data in enumerate(inputs, 1):
                    track = folder / f"Track {number}.bin"
                    track.write_bytes(data)
                    lines.append(f'FILE "{track.name}" BINARY\n TRACK {number:02d} AUDIO\n INDEX 01 00:00:00\n')
                cue.write_text("".join(lines), encoding="utf-8")
                roms = [DatRom(f"Track {number}.bin", len(data) + size_delta,
                               f"{zlib.crc32(data):08x}", hashlib.sha1(data).hexdigest())
                        for number, data in enumerate(expected, 1)]
                entry = DatEntry("Official Game (USA)", roms, system)
                index = DatIndex([entry], {}, {rom.sha1: [entry] for rom in roms if rom.sha1},
                                 {rom.crc: [entry] for rom in roms if rom.crc})
                workflow = ManifestWorkflow(FakeChdman(), index, CueRepository(Path(temporary) / "cues"))
                before = {path: path.read_bytes() for path in folder.iterdir()}
                status, details = workflow.process_file(cue)
                self.assertEqual("Completed", status)
                self.assertEqual(matched, "payload matches Redump DAT" in details)
                metadata = manifest_for(cue)
                assert metadata is not None
                manifest = load_manifest(metadata)
                assert manifest is not None
                self.assertEqual("Official Game (USA)" if matched else "Input", manifest.title)
                self.assertEqual("psx", manifest.system)
                self.assertEqual(len(inputs), len(manifest.disc_entries[0].payload_files))
                result = VerifyWorkflow(index).process_game_dir(folder, metadata)
                self.assertEqual("passed" if matched else "failed", result.status)
                for path, data in before.items():
                    self.assertEqual(data, path.read_bytes())

    def test_cd_chd_tracks_are_temporarily_extracted_and_hashed_as_a_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.chd"
            source.write_bytes(b"compressed CD")
            output_folders: list[Path] = []
            def split(input_chd: Path, cue: Path) -> str:
                output_folders.append(cue.parent)
                cue.write_text('FILE "Track 1.bin" BINARY\n TRACK 01 MODE2/2352\nFILE "Track 2.bin" BINARY\n TRACK 02 AUDIO\n', encoding="utf-8")
                (cue.parent / "Track 1.bin").write_bytes(b"first")
                (cue.parent / "Track 2.bin").write_bytes(b"second")
                return ""
            tool = FakeChdman()
            info = ChdInfo(100, 2, True, False, "")
            with patch.object(tool, "info", return_value=info), patch.object(tool, "splitbin", side_effect=split):
                status, _ = ManifestWorkflow(tool, None, CueRepository(folder)).process_file(source)
            self.assertEqual("Completed", status)
            self.assertFalse(output_folders[0].exists())
            self.assertEqual(11, item_for(source).original_size)
            manifest = load_manifest(folder / "Game.manifest.json")
            assert manifest is not None
            self.assertEqual("bin-cue", manifest.set_type)
            self.assertEqual(2, len(manifest.disc_entries[0].payload_files))
            self.assertEqual(b"compressed CD", source.read_bytes())

    def test_insufficient_temporary_space_fails_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.chd"
            source.write_bytes(b"original")
            tool = FakeChdman()
            with patch("redump_engine.workflow_manifest.shutil.disk_usage", return_value=SimpleNamespace(free=0)), patch.object(tool, "extractdvd") as extract:
                with self.assertRaisesRegex(ChdmanError, "Not enough free space"):
                    ManifestWorkflow(tool, None, CueRepository(folder)).process_file(source)
                extract.assert_not_called()
            self.assertEqual(b"original", source.read_bytes())
            self.assertFalse((folder / "Game.manifest.json").exists())
    def test_iso_manifest_records_real_payload_without_chdman_or_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.iso"
            source.write_bytes(b"original disc")
            tool = FakeChdman()
            with patch.object(tool, "info", side_effect=AssertionError("ISO must not use chdman")):
                status, _ = ManifestWorkflow(tool, None, CueRepository(folder)).process_file(source)
            self.assertEqual("Completed", status)
            manifest = load_manifest(folder / "Game.manifest.json")
            assert manifest is not None
            payload = manifest.disc_entries[0].payload_files[0]
            self.assertEqual(13, payload["size"])
            self.assertEqual(compute_hashes(source), (payload["sha1"], payload["crc32"]))
            self.assertEqual(b"original disc", source.read_bytes())
            self.assertFalse(source.with_suffix(".chd").exists())
            self.assertFalse(item_for(source).eligible("manifest"))

    def test_chd_is_extracted_only_temporarily_and_records_uncompressed_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.chd"
            source.write_bytes(b"compressed")
            temporary_outputs: list[Path] = []
            def extract(input_chd: Path, output: Path) -> str:
                self.assertEqual(source, input_chd)
                temporary_outputs.append(output)
                output.write_bytes(b"uncompressed payload" * 10)
                return ""
            tool = FakeChdman()
            with patch.object(tool, "extractdvd", side_effect=extract):
                status, _ = ManifestWorkflow(tool, None, CueRepository(folder)).process_file(source)
            self.assertEqual("Completed", status)
            self.assertEqual(b"compressed", source.read_bytes())
            self.assertFalse((folder / "Game.iso").exists())
            self.assertFalse(temporary_outputs[0].parent.exists())
            item = item_for(source)
            self.assertEqual(200, item.original_size)
            self.assertFalse(item.missing_record)

    def test_failed_chd_extraction_cleans_temporary_data_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.chd"
            source.write_bytes(b"original")
            outputs: list[Path] = []
            def fail(input_chd: Path, output: Path) -> str:
                outputs.append(output)
                output.write_bytes(b"partial")
                raise ChdmanError("extraction failed")
            tool = FakeChdman()
            with patch.object(tool, "extractdvd", side_effect=fail):
                with self.assertRaisesRegex(ChdmanError, "extraction failed"):
                    ManifestWorkflow(tool, None, CueRepository(folder)).process_file(source)
            self.assertFalse(outputs[0].parent.exists())
            self.assertEqual(b"original", source.read_bytes())
            self.assertFalse((folder / "Game.manifest.json").exists())

    def test_existing_record_is_not_rehashed_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.chd"
            source.write_bytes(b"chd")
            manifest_path = folder / "Game.manifest.json"
            save_manifest(manifest_path, Manifest(1, "iso", "Game", [ManifestDisc(1, [{"name": "Game.iso", "kind": "iso", "size": 99}])]))
            before = manifest_path.read_bytes()
            tool = FakeChdman()
            with patch.object(tool, "info", side_effect=AssertionError("must skip existing record")):
                status, _ = ManifestWorkflow(tool, None, CueRepository(folder)).process_file(source)
            self.assertEqual("Skipped", status)
            self.assertEqual(before, manifest_path.read_bytes())

    def test_creates_missing_disc_record_without_replacing_other_discs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            one = ManifestDisc(1, [{"name": "Game (Disc 1).iso", "kind": "iso", "size": 111}])
            manifest_path = folder / "Game.manifest.json"
            save_manifest(manifest_path, Manifest(1, "iso", "Game", [one]))
            source = folder / "Game (Disc 2).iso"
            source.write_bytes(b"disc two")
            self.assertTrue(item_for(source).eligible("manifest"))
            ManifestWorkflow(FakeChdman(), None, CueRepository(folder)).process_file(source)
            updated = load_manifest(manifest_path)
            assert updated is not None
            self.assertEqual([1, 2], [disc.disc for disc in updated.disc_entries])
            self.assertEqual(one, updated.disc_entries[0])

    def test_cue_tracks_are_hashed_together_without_removing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cue = folder / "Game.cue"
            cue.write_text('FILE "Track 1.bin" BINARY\n TRACK 01 MODE2/2352\nFILE "Track 2.bin" BINARY\n TRACK 02 AUDIO\n', encoding="utf-8")
            for number in (1, 2):
                (folder / f"Track {number}.bin").write_bytes(b"x" * number)
            ManifestWorkflow(FakeChdman(), None, CueRepository(folder)).process_file(cue)
            manifest = load_manifest(folder / "Game.manifest.json")
            assert manifest is not None
            self.assertEqual("bin-cue", manifest.set_type)
            self.assertEqual([1, 2], [payload["size"] for payload in manifest.disc_entries[0].payload_files])
            self.assertEqual(3, len([item for item in folder.iterdir() if item.suffix.lower() in {".cue", ".bin"}]))

    def test_dat_named_chd_keeps_original_filename_association(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "backup.chd"
            payload = b"disc payload"
            source.write_bytes(payload)
            dats = folder / "dats"
            dats.mkdir()
            (dats / "game.dat").write_text(_dat_xml("Canonical Game (USA)", "Canonical Game (USA).iso", payload), encoding="utf-8")
            status, _ = ManifestWorkflow(FakeChdman(), load_dat_index(dats), CueRepository(folder)).process_file(source)
            self.assertEqual("Completed", status)
            manifest_path = folder / "Canonical Game (USA).manifest.json"
            self.assertEqual(manifest_path, manifest_for(source))
            self.assertEqual(len(payload), item_for(source).original_size)
            self.assertFalse(item_for(source).missing_record)
            manifest = load_manifest(manifest_path)
            assert manifest is not None
            self.assertIn("backup.chd", manifest.disc_entries[0].source_files)

    def test_uncompressed_representations_are_preferred_before_chds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("Game.chd", "Game.iso"):
                (folder / name).touch()
            self.assertEqual([folder / "Game.iso", folder / "Game.chd"], selected_paths(scan_library(folder), "manifest"))

    def test_manifest_cli_dry_run_does_not_extract_or_write_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "Game.chd"
            source.write_bytes(b"original")
            with contextlib.redirect_stdout(io.StringIO()):
                code = run(["manifest", "--root", tmp, "--dry-run"])
            self.assertEqual(0, code)
            self.assertFalse((folder / "Game.manifest.json").exists())
            self.assertEqual(b"original", source.read_bytes())
