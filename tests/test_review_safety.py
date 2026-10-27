"""Regressions for conversion safety, complete matching, and reviewed approvals."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from threading import Event

import test_image_conversion
from test_hardening import FakeChdman

from redump_engine.cli import run
from redump_engine.dat_models import DatEntry, DatIndex, DatRom
from redump_engine.image_conversion import convert_image
from redump_engine.layout_review import prepare_layout_review
from redump_engine.metadata import Manifest, ManifestDisc, save_manifest
from redump_engine.workflow_convert import ConvertWorkflow
from redump_engine.workflow_verify import VerifyWorkflow


def payload(name, data):
    return {"name": name, "kind": "bin", "size": len(data), "sha1": hashlib.sha1(data).hexdigest(),
            "crc32": f"{zlib.crc32(data):08x}"}


def index_for(entries):
    return DatIndex(entries, {},
                    {checksum: [entry for entry in entries if checksum in entry.payload_sha1_set]
                     for entry in entries for checksum in entry.payload_sha1_set},
                    {checksum: [entry for entry in entries if checksum in entry.payload_crc_set]
                     for entry in entries for checksum in entry.payload_crc_set})


class ReviewSafetyTests(unittest.TestCase):
    def test_changed_iso_kept_and_chd_rolled_back(self):
        class ChangingTool(FakeChdman):
            def createdvd(self, input_image, output_chd):
                result = super().createdvd(input_image, output_chd)
                input_image.write_bytes(b"changed during conversion")
                return result
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Sony - PlayStation 2" / "Game"
            folder.mkdir(parents=True)
            source = folder / "Game.iso"
            source.write_bytes(b"original")
            output = folder / "Game.chd"
            output.write_bytes(b"previous output")
            workflow = ConvertWorkflow(ChangingTool(force=True), None, keep_sources=False, rename_folder=False)
            with self.assertRaisesRegex(ValueError, "Source changed"):
                workflow.process_game_dir(folder, [source])
            self.assertEqual(b"changed during conversion", source.read_bytes())
            self.assertEqual(b"previous output", output.read_bytes())
            self.assertFalse((folder / "manifest.json").exists())

    def test_changed_bin_kept_and_chd_rolled_back(self):
        class ChangingTool(FakeChdman):
            def createcd(self, input_image, output_chd):
                track = input_image.parent / "Track.bin"
                output_chd.write_bytes(track.read_bytes())
                track.write_bytes(b"changed track")
                return ""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Sony - PlayStation" / "Game"
            folder.mkdir(parents=True)
            cue = folder / "Game.cue"
            cue.write_text('FILE "Track.bin" BINARY\n TRACK 01 MODE1/2352\n INDEX 01 00:00:00\n')
            track = folder / "Track.bin"
            track.write_bytes(b"original track")
            workflow = ConvertWorkflow(ChangingTool(), None, keep_sources=False, rename_folder=False)
            with self.assertRaisesRegex(ValueError, "Source changed"):
                workflow.process_game_dir(folder, [cue])
            self.assertTrue(cue.exists())
            self.assertEqual(b"changed track", track.read_bytes())
            self.assertFalse((folder / "Game.chd").exists())

    def test_naming_failure_restores_local_and_system_playlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output, manifests, convert = test_image_conversion.ImageConversionTests().fixture(Path(tmp), "Nintendo - GameCube", ".rvz")
            local, system = source.parent / "Game.m3u", source.parent.parent / "System.m3u"
            local.write_text(source.name + "\n")
            system.write_text(source.parent.name + "/" + source.name + "\n")
            originals = {path: path.read_bytes() for path in (local, system)}
            source.write_bytes(b"disc with no DAT match")
            def convert_with_playlists(managed):
                from redump_engine.playlists import playlist_updates
                result = convert(managed)
                for path, content in playlist_updates(source.parent, source.parent, {source: output}).items():
                    path.write_bytes(content)
                return result
            with self.assertRaises(ValueError):
                convert_image(source, output, manifests, convert_with_playlists, use_redump_names=True, keep_sources=False)
            self.assertTrue(source.exists())
            self.assertFalse(output.exists())
            for path, content in originals.items():
                self.assertEqual(content, path.read_bytes())

    def test_manifest_requires_complete_count_sizes_and_unique_system_match(self):
        one, two = payload("Track 1.bin", b"one"), payload("Track 2.bin", b"second")
        def rom(value):
            return DatRom(value["name"], value["size"], value["crc32"], value["sha1"])
        two_track = DatEntry("Game", [rom(one), rom(two)], "psx")
        duplicate = DatEntry("Game", [rom(one), rom(one)], "psx")
        wrong_size = {**one, "size": one["size"] + 1}
        cases = [([one], [two_track], "failed"), ([one, two], [two_track], "passed"),
                 ([wrong_size, two], [two_track], "failed"), ([one], [duplicate], "failed"),
                 ([one, one], [duplicate], "passed"),
                 ([one], [DatEntry("A", [rom(one)], "psx"), DatEntry("B", [rom(one)], "psx")], "failed"),
                 ([one], [DatEntry("Game", [rom(one)], "psx"), DatEntry("Other", [rom(one)], "dc")], "passed")]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for payloads, entries, expected in cases:
                with self.subTest(expected=expected, count=len(payloads)):
                    save_manifest(folder / "manifest.json", Manifest(1, "bin-cue", "Game", [ManifestDisc(1, payloads)], "psx"))
                    self.assertEqual(expected, VerifyWorkflow(index_for(entries)).process_game_dir(folder).status)

    def test_cli_rejects_changed_plan_and_accepts_unchanged_approval(self):
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, output, manifests, convert = test_image_conversion.ImageConversionTests().fixture(root, "Sony - PlayStation 2", ".chd")
                manifests.process_file(source)
                review = prepare_layout_review([source], root / "dats", Event(), lambda _: None, organize=True)
                assert review is not None
                selection, approval = root / "selection.json", root / "approved.json"
                selection.write_text(json.dumps([str(source)]))
                approval.write_text(json.dumps(review.approved_plans))
                if changed:
                    source.write_bytes(b"changed after approval")
                with contextlib.redirect_stdout(io.StringIO()) as output_log:
                    code = run(["--dats", str(root / "dats"), "--cuesheets", str(root / "cues"), "organize", "--root", str(root),
                                "--file-list", str(selection), "--approved-plans", str(approval)])
                self.assertEqual(2 if changed else 0, code, output_log.getvalue())
                if changed:
                    self.assertTrue(source.exists())
                    self.assertFalse((source.parent.parent / "Official (USA)").exists())
                else:
                    self.assertTrue((source.parent.parent / "Official (USA)").exists())

    def test_cached_dat_checks_expire_when_matching_rules_change(self):
        from redump_engine.actions import VerificationMode
        from redump_engine.verification_cache import VerificationRecords
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            dats.mkdir()
            image = root / "Game.iso"
            image.write_bytes(b"disc")
            store = VerificationRecords()
            store.remember(image, VerificationMode.ORIGINALS, "Original files: Passed", dats)
            self.assertEqual("Original files: Passed", store.lookup(image, dats))
            store.records[str(image.resolve())]["inputs"].pop("rules")
            self.assertIsNone(store.lookup(image, dats))

    def test_shared_iso_xiso_organization_is_reviewed_and_executed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output, manifests, convert = test_image_conversion.ImageConversionTests().fixture(root, "Microsoft - Xbox", ".xiso.iso")
            convert(manifests)
            review = prepare_layout_review([source, output], root / "dats", Event(), lambda _: None, organize=True)
            assert review is not None
            self.assertEqual(1, review.planned)
            self.assertEqual(1, len(review.approved_plans))
            selection, approval = root / "selection.json", root / "approval.json"
            selection.write_text(json.dumps(list(review.approved_plans)))
            approval.write_text(json.dumps(review.approved_plans))
            with contextlib.redirect_stdout(io.StringIO()) as log:
                code = run(["--dats", str(root / "dats"), "organize", "--root", str(root), "--file-list", str(selection), "--approved-plans", str(approval)])
            self.assertEqual(0, code, log.getvalue())
            target = root / "Microsoft - Xbox" / "Official (USA)"
            self.assertTrue((target / "Official (USA).iso").exists())
            self.assertTrue((target / "Official (USA).xiso.iso").exists())
            self.assertNotIn("FAILED", log.getvalue())

    def test_loose_iso_xiso_share_one_approved_plan_and_retain_unrelated_disc(self):
        from redump_engine.library import manifest_for
        from redump_engine.metadata import load_manifest, payload_files_from_paths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output, manifests, convert = test_image_conversion.ImageConversionTests().fixture(root, "Microsoft - Xbox", ".xiso.iso")
            convert(manifests)
            old, platform = source.parent, source.parent.parent
            for file in list(old.iterdir()):
                file.rename(platform / file.name)
            old.rmdir()
            source, output = platform / source.name, platform / output.name
            metadata = manifest_for(source)
            assert metadata is not None
            other = platform / "Other.iso"
            other.write_bytes(b"unrelated game")
            manifest = load_manifest(metadata)
            assert manifest is not None
            manifest.disc_entries.append(ManifestDisc(2, payload_files_from_paths([other], "iso"), [other.name]))
            save_manifest(metadata, manifest)
            review = prepare_layout_review([source, output], root / "dats", Event(), lambda _: None, organize=True)
            assert review is not None
            self.assertEqual(1, review.planned)
            self.assertEqual(1, len(review.approved_plans))
            selection, approval = root / "files.json", root / "approval.json"
            selection.write_text(json.dumps(list(review.approved_plans)))
            approval.write_text(json.dumps(review.approved_plans))
            with contextlib.redirect_stdout(io.StringIO()) as log:
                code = run(["--dats", str(root / "dats"), "organize", "--root", str(root), "--file-list", str(selection), "--approved-plans", str(approval)])
            self.assertEqual(0, code, log.getvalue())
            destination = platform / "Official (USA)"
            self.assertTrue((destination / "Official (USA).iso").exists())
            self.assertTrue((destination / "Official (USA).xiso.iso").exists())
            combined = load_manifest(destination / "manifest.json")
            retained = load_manifest(metadata)
            assert combined is not None and retained is not None
            self.assertEqual({"Official (USA).iso", "Official (USA).xiso.iso"}, set(combined.disc_entries[0].source_files))
            self.assertEqual([2], [disc.disc for disc in retained.disc_entries])
            self.assertEqual(b"unrelated game", other.read_bytes())

    def test_nested_tracks_record_recheck_convert_and_expire_correctly(self):
        import test_conversion_naming

        from redump_engine.actions import VerificationMode
        from redump_engine.chd_ops import Chdman
        from redump_engine.cuesheets import CueRepository
        from redump_engine.library import manifest_for
        from redump_engine.metadata import load_dat_index, load_manifest
        from redump_engine.verification_cache import VerificationRecords
        from redump_engine.workflow_manifest import ManifestWorkflow
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Sony - PlayStation" / "Game"
            tracks = []
            for name, content in (("a", b"first track"), ("b", b"second track")):
                subfolder = folder / name
                subfolder.mkdir(parents=True)
                track = subfolder / "Track.bin"
                track.write_bytes(content)
                tracks.append(track)
            cue = folder / "Game.cue"
            cue.write_text('FILE "a/Track.bin" BINARY\n TRACK 01 MODE1/2352\n INDEX 01 00:00:00\nFILE "b/Track.bin" BINARY\n TRACK 02 AUDIO\n INDEX 01 00:00:00\n')
            dats = root / "dats"
            dats.mkdir()
            entries = test_conversion_naming.dat_game("Game", {f"Game (Track {number}).bin": track.read_bytes() for number, track in enumerate(tracks, 1)})
            (dats / "psx.dat").write_text('<datafile><header><name>Sony - PlayStation</name></header>' + entries + '</datafile>')
            index = load_dat_index(dats)
            manifests = ManifestWorkflow(Chdman(Path("unused")), index, CueRepository(root / "cues"))
            manifests.process_file(cue)
            metadata = manifest_for(cue)
            assert metadata is not None
            manifest = load_manifest(metadata)
            assert manifest is not None
            self.assertEqual(["a/Track.bin", "b/Track.bin"], [value["name"] for value in manifest.disc_entries[0].payload_files])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual("passed", VerifyWorkflow(index, rehash=True).process_game_dir(folder, metadata).status)
            workflow = ConvertWorkflow(test_conversion_naming.NamingTool(), index, keep_sources=True, rename_folder=False)
            workflow.process_game_dir(folder, [cue])
            converted = load_manifest(metadata)
            assert converted is not None
            self.assertEqual(["a/Track.bin", "b/Track.bin"], [value["name"] for value in converted.disc_entries[0].payload_files])
            cache = VerificationRecords()
            cache.remember(metadata, VerificationMode.ORIGINALS, "Original files: Passed", dats)
            self.assertEqual("Original files: Passed", cache.lookup(metadata, dats))
            tracks[1].write_bytes(b"changed nested track")
            self.assertIsNone(cache.lookup(metadata, dats))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual("failed", VerifyWorkflow(index, rehash=True).process_game_dir(folder, metadata).status)

    def test_payload_path_resolution_rejects_escape(self):
        from redump_engine.payload_paths import resolve_payload
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "game"
            folder.mkdir()
            with self.assertRaisesRegex(ValueError, "escapes"):
                resolve_payload(folder, "../outside.bin")

    def test_combined_loose_plan_rolls_back_all_images_metadata_and_playlist(self):
        from unittest.mock import patch

        from redump_engine.file_transaction import FileTransaction
        from redump_engine.metadata import load_dat_index
        from redump_engine.workflow_organize import plan_organization
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output, manifests, convert = test_image_conversion.ImageConversionTests().fixture(root, "Microsoft - Xbox", ".xiso.iso")
            convert(manifests)
            old, platform = source.parent, source.parent.parent
            for file in list(old.iterdir()):
                file.rename(platform / file.name)
            old.rmdir()
            source, output = platform / source.name, platform / output.name
            playlist = platform / "Games.m3u"
            playlist.write_text(source.name + "\n" + output.name + "\n")
            before = {file.relative_to(platform): file.read_bytes() for file in platform.rglob("*") if file.is_file()}
            plan = plan_organization(source, load_dat_index(root / "dats"))
            transfer = FileTransaction.transfer
            calls = 0
            def fail(transaction, old, new, *, copy=False, source_guard=None):
                nonlocal calls
                transfer(transaction, old, new, copy=copy, source_guard=source_guard)
                calls += 1
                if calls == 2:
                    raise OSError("simulated mid-game failure")
            with patch.object(FileTransaction, "transfer", fail):
                with self.assertRaisesRegex(OSError, "mid-game failure"):
                    plan.apply()
            after = {file.relative_to(platform): file.read_bytes() for file in platform.rglob("*") if file.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(plan.folder.exists())

    def test_independent_loose_games_keep_valid_approvals_in_one_batch(self):
        import test_conversion_naming

        from redump_engine.chd_ops import Chdman
        from redump_engine.cuesheets import CueRepository
        from redump_engine.metadata import load_dat_index
        from redump_engine.workflow_manifest import ManifestWorkflow
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            platform = root / "Microsoft - Xbox"
            platform.mkdir()
            dats = root / "dats"
            dats.mkdir()
            images = []
            entries = []
            for title in ("First (USA)", "Second (USA)"):
                image = platform / (title + ".iso")
                image.write_bytes(title.encode())
                images.append(image)
                entries.append(test_conversion_naming.dat_game(title, {image.name: image.read_bytes()}))
            (dats / "xbox.dat").write_text('<datafile><header><name>Microsoft - Xbox</name></header>' + ''.join(entries) + '</datafile>')
            manifests = ManifestWorkflow(Chdman(root / "unused"), load_dat_index(dats), CueRepository(root / "cues"))
            for image in images:
                manifests.process_file(image)
            review = prepare_layout_review(images, dats, Event(), lambda _: None, organize=True)
            assert review is not None
            self.assertEqual(2, review.planned)
            selection, approval = root / "files.json", root / "approval.json"
            selection.write_text(json.dumps(list(review.approved_plans)))
            approval.write_text(json.dumps(review.approved_plans))
            with contextlib.redirect_stdout(io.StringIO()) as log:
                code = run(["--dats", str(dats), "organize", "--root", str(root), "--file-list", str(selection), "--approved-plans", str(approval)])
            self.assertEqual(0, code, log.getvalue())
            for image in images:
                self.assertTrue((platform / image.stem / image.name).exists())

    def test_same_size_source_change_with_restored_timestamp_rolls_back(self):
        import os
        class PreservedTimestampTool(FakeChdman):
            def createdvd(self, input_image, output_chd):
                before = input_image.stat()
                result = super().createdvd(input_image, output_chd)
                input_image.write_bytes(b"modified data")
                os.utime(input_image, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Sony - PlayStation 2" / "Game"
            folder.mkdir(parents=True)
            source = folder / "Game.iso"
            source.write_bytes(b"original data")
            output = folder / "Game.chd"
            output.write_bytes(b"previous compressed output")
            workflow = ConvertWorkflow(PreservedTimestampTool(force=True), None, keep_sources=False, rename_folder=False)
            with self.assertRaisesRegex(ValueError, "Source changed"):
                workflow.process_game_dir(folder, [source])
            self.assertEqual(b"modified data", source.read_bytes())
            self.assertEqual(b"previous compressed output", output.read_bytes())
            self.assertFalse((folder / "manifest.json").exists())
