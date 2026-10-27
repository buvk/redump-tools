"""Restoration must preserve existing files and changed CHD inputs on failure."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import ChdInfo, Chdman, ChdmanError
from redump_engine.cuesheets import CueRepository
from redump_engine.file_io import write_metadata
from redump_engine.library import manifest_for
from redump_engine.metadata import Manifest, ManifestDisc, load_manifest, payload_files_from_paths, save_manifest
from redump_engine.workflow_extract import ExtractWorkflow


class RestoreTool(Chdman):
    def __init__(self, *, force=True, fail=False, change=False, cd=False):
        super().__init__(Path("unused"), force=force)
        self.fail, self.change, self.cd = fail, change, cd

    def info(self, input_chd: Path):
        return ChdInfo(1000 if self.cd else 1_000_000_000, 2 if self.cd else 0, self.cd, not self.cd, "")

    def extractdvd(self, input_chd: Path, output_file: Path):
        output_file.write_bytes(b"restored image")
        if self.change:
            stat = input_chd.stat()
            input_chd.write_bytes(b"changed CHD!")
            os.utime(input_chd, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        if self.fail:
            raise ChdmanError("simulated tool failure")
        return ""

    def extractcd(self, input_chd: Path, output_file: Path):
        output_file.write_text(f'FILE "{output_file.with_suffix(".bin").name}" BINARY\n TRACK 01 MODE1/2352\n INDEX 01 00:00:00\n')
        output_file.with_suffix(".bin").write_bytes(b"restored track")
        return ""

    def splitbin(self, input_chd: Path, output_cue: Path):
        return self.extractcd(input_chd, output_cue)


class ExtractSafetyTests(unittest.TestCase):
    def fixture(self, root, tool, *, manifest=True):
        folder = root / "Game"
        folder.mkdir()
        source = folder / "Game.chd"
        source.write_bytes(b"original CHD")
        output = folder / "Game.iso"
        output.write_bytes(b"previous good ISO")
        if manifest:
            save_manifest(folder / "manifest.json", Manifest(1, "iso", "Game", [ManifestDisc(1, [], [source.name])]))
        workflow = ExtractWorkflow(tool, None, CueRepository(root / "cues"), keep_sources=False, rename_folder=False)
        return folder, source, output, workflow

    def test_unnumbered_disc_two_uses_its_manifest_record_and_format(self):
        for cd in (False, True):
            for keep_source in (False, True):
                with self.subTest(cd=cd, keep_source=keep_source), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    folder = root / "Game"
                    folder.mkdir()
                    source = folder / "backup.chd"
                    source.write_bytes(b"original CHD")
                    metadata = folder / "manifest.json"
                    first = ManifestDisc(1, [{"name": "First.iso", "kind": "iso", "size": 1000}], ["First.chd"])
                    second = ManifestDisc(2, [{"name": "Second.bin" if cd else "Second.iso", "kind": "bin" if cd else "iso", "size": 2000}], [source.name])
                    save_manifest(metadata, Manifest(1, "mixed" if cd else "iso", "Game", [first, second], "ps2"))
                    playlist = folder / "Game.m3u"
                    playlist.write_text("First.chd\nbackup.chd\n")
                    tool = RestoreTool(cd=cd)
                    workflow = ExtractWorkflow(tool, None, CueRepository(root / "cues"), keep_sources=keep_source, rename_folder=False)
                    with patch.object(tool, "extractdvd", wraps=tool.extractdvd) as dvd, patch.object(tool, "extractcd", wraps=tool.extractcd) as extract_cd:
                        reports = workflow.process_game_dir(folder, selected_file=source, input_manifest=metadata)
                    self.assertEqual(0 if cd else 1, dvd.call_count)
                    self.assertEqual(1 if cd else 0, extract_cd.call_count)
                    updated = load_manifest(metadata)
                    assert updated is not None
                    self.assertEqual([1, 2], [disc.disc for disc in updated.disc_entries])
                    self.assertEqual(first, updated.disc_entry_for(1))
                    restored = updated.disc_entry_for(2)
                    assert restored is not None
                    self.assertIn(source.name, restored.source_files)
                    self.assertEqual("bin" if cd else "iso", restored.payload_files[0]["kind"])
                    extension = "cue" if cd else "iso"
                    self.assertIn(f"backup.{extension}", restored.source_files)
                    self.assertTrue(any(path.name == f"backup.{extension}" for path in reports[0].extracted_files))
                    self.assertEqual(keep_source, source.exists())
                    self.assertEqual("First.chd\n" + ("backup.chd" if keep_source else f"backup.{extension}") + "\n", playlist.read_text())

    def test_ambiguous_unnumbered_chd_does_not_modify_a_multi_disc_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Game"
            folder.mkdir()
            source = folder / "unassigned.chd"
            source.write_bytes(b"original CHD")
            metadata = folder / "manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [
                ManifestDisc(number, [{"name": f"Disc {number}.iso", "kind": "iso", "size": 1000}], [f"Disc {number}.chd"])
                for number in (1, 2)]))
            before = {path: path.read_bytes() for path in folder.iterdir()}
            tool = RestoreTool()
            workflow = ExtractWorkflow(tool, None, CueRepository(root / "cues"), keep_sources=False, rename_folder=False)
            with patch.object(tool, "info") as info, patch.object(tool, "extractdvd") as extract:
                with self.assertRaisesRegex(ChdmanError, "Cannot identify this CHD"):
                    workflow.process_game_dir(folder, selected_file=source, input_manifest=metadata)
            info.assert_not_called()
            extract.assert_not_called()
            self.assertEqual(before, {path: path.read_bytes() for path in folder.iterdir()})

    def test_restoration_preserves_other_images_of_the_same_original_disc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, source, output, workflow = self.fixture(root, RestoreTool())
            alternate = folder / "backup.chd"
            alternate.write_bytes(b"another representation")
            output.write_bytes(b"restored image")
            metadata = folder / "manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1,
                payload_files_from_paths([output], "iso"), [source.name, alternate.name, output.name])]))
            self.assertEqual(metadata, manifest_for(alternate))
            workflow.keep_sources = True
            workflow.process_game_dir(folder, selected_file=source, input_manifest=metadata)
            updated = load_manifest(metadata)
            assert updated is not None
            self.assertIn(alternate.name, updated.disc_entries[0].source_files)
            self.assertEqual(metadata, manifest_for(alternate))
            self.assertEqual(b"another representation", alternate.read_bytes())

    def test_failed_tool_preserves_existing_output_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool(fail=True))
            before = {path: path.read_bytes() for path in folder.iterdir()}
            with self.assertRaisesRegex(ChdmanError, "simulated tool failure"):
                workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual(before, {path: path.read_bytes() for path in folder.iterdir()})

    def test_changed_source_with_restored_timestamp_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool(change=True))
            original_manifest = (folder / "manifest.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "Source changed"):
                workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual(b"changed CHD!", source.read_bytes())
            self.assertEqual(b"previous good ISO", output.read_bytes())
            self.assertEqual(original_manifest, (folder / "manifest.json").read_bytes())
            self.assertEqual(3, len(list(folder.iterdir())))

    def test_metadata_failure_rolls_back_published_image_and_new_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool(), manifest=False)
            write_manifest = workflow._write_extract_manifest
            def fail_after_write(*args, **kwargs):
                write_manifest(*args, **kwargs)
                raise OSError("metadata failure")
            with patch.object(workflow, "_write_extract_manifest", side_effect=fail_after_write):
                with self.assertRaisesRegex(OSError, "metadata failure"):
                    workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual(b"original CHD", source.read_bytes())
            self.assertEqual(b"previous good ISO", output.read_bytes())
            self.assertEqual({source, output}, set(folder.iterdir()))

    def test_source_change_after_publication_rolls_back_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool())
            before_metadata = (folder / "manifest.json").read_bytes()
            write_manifest = workflow._write_extract_manifest
            def change_after_write(*args, **kwargs):
                write_manifest(*args, **kwargs)
                stat = source.stat()
                source.write_bytes(b"changed CHD!")
                os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            with patch.object(workflow, "_write_extract_manifest", side_effect=change_after_write):
                with self.assertRaisesRegex(ValueError, "Source changed"):
                    workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual(b"changed CHD!", source.read_bytes())
            self.assertEqual(b"previous good ISO", output.read_bytes())
            self.assertEqual(before_metadata, (folder / "manifest.json").read_bytes())
            self.assertEqual(3, len(list(folder.iterdir())))

    def test_named_manifest_creation_rolls_back_in_system_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool(), manifest=False)
            system_folder = folder.with_name("Sony - PlayStation 2")
            folder.rename(system_folder)
            source, output = system_folder / source.name, system_folder / output.name
            write_manifest = workflow._write_extract_manifest
            def fail_after_write(*args, **kwargs):
                write_manifest(*args, **kwargs)
                self.assertTrue((system_folder / "Game.manifest.json").exists())
                raise OSError("metadata failure")
            with patch.object(workflow, "_write_extract_manifest", side_effect=fail_after_write):
                with self.assertRaisesRegex(OSError, "metadata failure"):
                    workflow.process_game_dir(system_folder, selected_file=source)
            self.assertEqual({source, output}, set(system_folder.iterdir()))
            self.assertEqual(b"previous good ISO", output.read_bytes())

    def test_existing_output_requires_overwrite_and_success_removes_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool(force=False))
            with self.assertRaisesRegex(ChdmanError, "Refusing to overwrite"):
                workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual(b"previous good ISO", output.read_bytes())
            self.assertTrue(source.exists())
            workflow.chdman.force = True
            reports = workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual([output], reports[0].extracted_files)
            self.assertEqual(b"restored image", output.read_bytes())
            self.assertFalse(source.exists())
            manifest = load_manifest(folder / "manifest.json")
            self.assertIsNotNone(manifest)
            assert manifest is not None
            self.assertEqual("Game.iso", manifest.disc_entries[0].payload_files[0]["name"])
            self.assertEqual({output, folder / "manifest.json"}, set(folder.iterdir()))

    def test_restoration_updates_local_and_parent_playlists(self):
        for cd in (False, True):
            for keep_source in (False, True):
                for selected in (False, True):
                    with self.subTest(cd=cd, keep_source=keep_source, selected=selected), tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        folder, source, _, workflow = self.fixture(root, RestoreTool(cd=cd), manifest=not cd)
                        workflow.keep_sources = keep_source
                        local, parent = folder / "Custom.m3u", root / "Collection.m3u"
                        local.write_bytes(b"\xef\xbb\xbf# Local set\r\nGame.chd\r\nOther.chd\r\n")
                        parent.write_bytes(b"# Collection\nGame/Game.chd\nOther/Other.chd\n")
                        workflow.process_game_dir(folder, selected_file=source if selected else None)
                        restored_extension = "cue" if cd else "iso"
                        extension = "chd" if keep_source else restored_extension
                        self.assertEqual(f"# Local set\r\nGame.{extension}\r\nOther.chd\r\n", local.read_bytes().decode("utf-8-sig"))
                        self.assertTrue(local.read_bytes().startswith(b"\xef\xbb\xbf"))
                        self.assertEqual(f"# Collection\nGame/Game.{extension}\nOther/Other.chd\n", parent.read_text())
                        self.assertTrue((folder / f"Game.{restored_extension}").exists())
                        self.assertEqual(keep_source, source.exists())

    def test_playlist_write_failure_restores_outputs_metadata_and_playlists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder, source, _, workflow = self.fixture(root, RestoreTool())
            local, parent = folder / "Game.m3u", root / "Collection.m3u"
            local.write_bytes(b"Game.chd\n")
            parent.write_bytes(b"Game/Game.chd\n")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            write_bytes = write_metadata
            failed = False
            def fail_playlist(path, content):
                nonlocal failed
                result = write_bytes(path, content)
                if path == parent and not failed:
                    failed = True
                    raise OSError("playlist write failed")
                return result
            with patch("redump_engine.workflow_extract.write_metadata", side_effect=fail_playlist):
                with self.assertRaisesRegex(OSError, "playlist write failed"):
                    workflow.process_game_dir(folder, selected_file=source)
            self.assertTrue(failed)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_partial_cd_publication_restores_both_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, source, output, workflow = self.fixture(Path(temporary), RestoreTool(cd=True), manifest=False)
            output.unlink()
            cue, track = folder / "Game.cue", folder / "Game.bin"
            cue.write_bytes(b"old cue")
            track.write_bytes(b"old track")
            from redump_engine.file_transaction import FileTransaction
            transfer = FileTransaction.transfer
            def fail_track(transaction, incoming, target, **kwargs):
                if target == track:
                    raise OSError("publish failure")
                return transfer(transaction, incoming, target, **kwargs)
            with patch.object(FileTransaction, "transfer", fail_track):
                with self.assertRaisesRegex(OSError, "publish failure"):
                    workflow.process_game_dir(folder, selected_file=source)
            self.assertEqual(b"old cue", cue.read_bytes())
            self.assertEqual(b"old track", track.read_bytes())
            self.assertTrue(source.exists())
            self.assertEqual({source, cue, track}, set(folder.iterdir()))
