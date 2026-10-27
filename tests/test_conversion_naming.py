from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from test_hardening import FakeChdman

from redump_engine.chd_ops import Chdman, ChdmanError
from redump_engine.cli import run
from redump_engine.cue import bin_files_from_cue
from redump_engine.library import item_for, manifest_for
from redump_engine.metadata import Manifest, ManifestDisc, load_dat_index, load_manifest, payload_files_from_paths, save_manifest
from redump_engine.ui_runner import build_command
from redump_engine.workflow_convert import ConvertWorkflow


class NamingTool(FakeChdman):
    def createcd(self, input_image: Path, output_chd: Path) -> str:
        output_chd.write_bytes(b"converted")
        return ""


def dat_game(title: str, payloads: dict[str, bytes]) -> str:
    roms = "".join(f'<rom name="{name}" size="{len(data)}" sha1="{hashlib.sha1(data).hexdigest()}" '
                   f'crc="{zlib.crc32(data) & 0xFFFFFFFF:08x}"/>' for name, data in payloads.items())
    return f'<game name="{title}">{roms}</game>'


class ConversionNamingTests(unittest.TestCase):
    def setup_separate_discs(self, root: Path) -> tuple[Path, Path, Path, Path]:
        platform = root / "Sega - Dreamcast"
        dats = root / "dats"
        dats.mkdir()
        cues, entries = [], []
        for disc in (1, 2):
            title = f"Canonical (USA) (Disc {disc})"
            folder = platform / title
            folder.mkdir(parents=True)
            cue = folder / f"{title}.cue"
            track = folder / f"{title}.bin"
            payload = f"disc {disc}".encode()
            track.write_bytes(payload)
            cue.write_text(f'FILE "{track.name}" BINARY\n TRACK 01 MODE1/2352\n', encoding="utf-8")
            cues.append(cue)
            entries.append(dat_game(title, {track.name: payload}))
        (dats / "dc.dat").write_text('<datafile><header><name>Sega - Dreamcast</name></header>' + "".join(entries) + '</datafile>', encoding="utf-8")
        return cues[0], cues[1], platform / "Canonical (USA)", dats

    def test_discs_in_separate_folders_combine_in_one_game_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two, target, dats = self.setup_separate_discs(root)
            workflow = ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=True, use_redump_names=True)
            for cue in (one, two):
                report = workflow.process_game_dir(cue.parent, [cue])
                self.assertEqual(str(target), report["game_dir"])
                self.assertTrue(all(Path(path).is_file() for path in report["created"]))
                self.assertFalse(cue.parent.exists())
            metadata = load_manifest(target / "manifest.json")
            assert metadata is not None
            self.assertEqual([1, 2], [disc.disc for disc in metadata.disc_entries])
            self.assertEqual(2, len(list(target.glob("*.chd"))))
            self.assertEqual(2, len(list(target.glob("*.cue"))))
            self.assertEqual(2, len(list(target.glob("*.bin"))))
            self.assertEqual(["Canonical (USA) (Disc 1).chd", "Canonical (USA) (Disc 2).chd"],
                             (target / "Canonical (USA).m3u").read_text().splitlines())

    def test_combining_disc_folders_never_overwrites_colliding_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two, target, dats = self.setup_separate_discs(root)
            workflow = ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=True, use_redump_names=True)
            workflow.process_game_dir(one.parent, [one])
            (target / "notes.txt").write_bytes(b"first notes")
            (two.parent / "notes.txt").write_bytes(b"second notes")
            before = {path.name: path.read_bytes() for path in target.iterdir()}
            with patch.object(workflow.chdman, "createcd") as create, self.assertRaisesRegex(ChdmanError, "file already exists"):
                workflow.process_game_dir(two.parent, [two])
            create.assert_not_called()
            self.assertTrue(two.is_file())
            self.assertEqual(before, {path.name: path.read_bytes() for path in target.iterdir()})

    def test_failed_consolidation_restores_both_disc_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two, target, dats = self.setup_separate_discs(root)
            workflow = ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=False, use_redump_names=True)
            workflow.process_game_dir(one.parent, [one])
            before_target = {path.name: path.read_bytes() for path in target.iterdir()}
            before_source = {path.name: path.read_bytes() for path in two.parent.iterdir()}
            rmdir = Path.rmdir

            def locked(path: Path) -> None:
                if path == two.parent:
                    raise PermissionError("source folder locked")
                rmdir(path)

            with patch.object(Path, "rmdir", locked), self.assertRaisesRegex(ChdmanError, "rolled back"):
                workflow.process_game_dir(two.parent, [two])
            self.assertEqual(before_target, {path.name: path.read_bytes() for path in target.iterdir()})
            self.assertEqual(before_source, {path.name: path.read_bytes() for path in two.parent.iterdir()})

    def setup_game(self, root: Path) -> tuple[Path, Path, Path]:
        folder = root / "Sega - Dreamcast" / "My Source Name"
        folder.mkdir(parents=True)
        cue = folder / "My Source Name.cue"
        cue.write_text('FILE "track01.bin" BINARY\n TRACK 01 MODE1/2352\n'
                       'FILE "track02.bin" BINARY\n TRACK 02 AUDIO\n', encoding="utf-8")
        payloads = {"track01.bin": b"one", "track02.bin": b"two"}
        for name, data in payloads.items():
            (folder / name).write_bytes(data)
        dats = root / "dats"
        dats.mkdir()
        (dats / "dc.dat").write_text('<datafile><header><name>Sega - Dreamcast</name></header>' +
                                     dat_game("Canonical Game (USA)", payloads) + '</datafile>', encoding="utf-8")
        return folder, cue, dats

    def test_overwrite_and_redump_naming_work_together_and_roll_back_on_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder, cue, dats = self.setup_game(root)
                existing = folder / "Canonical Game (USA).chd"
                existing.write_bytes(b"previous CHD")
                index = load_dat_index(dats)
                with self.assertRaisesRegex(ChdmanError, "--force"):
                    ConvertWorkflow(NamingTool(), index, keep_sources=True, use_redump_names=True).process_game_dir(folder, [cue])
                tool = NamingTool(force=True)
                workflow = ConvertWorkflow(tool, index, keep_sources=True, use_redump_names=True)
                if fail:
                    with patch.object(tool, "verify", side_effect=ChdmanError("verification failed")):
                        with self.assertRaisesRegex(ChdmanError, "verification failed"):
                            workflow.process_game_dir(folder, [cue])
                    self.assertEqual(b"previous CHD", existing.read_bytes())
                    self.assertTrue(cue.exists())
                    self.assertFalse((folder / "manifest.json").exists())
                else:
                    report = workflow.process_game_dir(folder, [cue])
                    target = Path(report["game_dir"])
                    self.assertEqual("Canonical Game (USA)", target.name)
                    self.assertEqual(b"converted", (target / existing.name).read_bytes())
                    self.assertFalse(folder.exists())
                self.assertFalse(list(root.rglob(".rollback_*")))

    def test_overwriting_disc_while_merging_folders_keeps_backups_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one, two, target, dats = self.setup_separate_discs(root)
            workflow = ConvertWorkflow(NamingTool(force=True), load_dat_index(dats), keep_sources=True, use_redump_names=True)
            workflow.process_game_dir(one.parent, [one])
            existing = two.with_suffix(".chd")
            existing.write_bytes(b"previous second disc")
            workflow.process_game_dir(two.parent, [two])
            self.assertEqual(b"converted", (target / existing.name).read_bytes())
            self.assertFalse(two.parent.exists())
            self.assertFalse(list(root.rglob(".rollback_*")))
            manifest = load_manifest(target / "manifest.json")
            assert manifest is not None
            self.assertEqual([1, 2], [disc.disc for disc in manifest.disc_entries])

    def test_folder_rename_refuses_external_cue_references_before_conversion(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, cue, dats = self.setup_game(Path(temporary))
            other = folder.parent / "Other.cue"
            other.write_text(f'FILE "{folder.name}/track01.bin" BINARY\n TRACK 01 MODE1/2352\n')
            before = {path: path.read_bytes() for path in folder.parent.rglob("*") if path.is_file()}
            tool = NamingTool()
            workflow = ConvertWorkflow(tool, load_dat_index(dats), keep_sources=False, use_redump_names=True)
            with patch.object(tool, "createcd") as create:
                with self.assertRaisesRegex(ChdmanError, "other CUE files reference"):
                    workflow.process_game_dir(folder, [cue])
            create.assert_not_called()
            self.assertEqual(before, {path: path.read_bytes() for path in folder.parent.rglob("*") if path.is_file()})

    def test_default_preserves_file_and_folder_but_records_dat_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, dats = self.setup_game(root)
            report = ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=True).process_game_dir(folder, [cue])
            output = folder / "My Source Name.chd"
            self.assertEqual([str(output)], report["created"])
            self.assertTrue(output.exists())
            self.assertFalse((folder.parent / "Canonical Game (USA)").exists())
            metadata = manifest_for(output)
            self.assertEqual(folder / "manifest.json", metadata)
            assert metadata is not None
            manifest = load_manifest(metadata)
            assert manifest is not None
            self.assertEqual("Canonical Game (USA)", manifest.title)
            self.assertIn(output.name, manifest.disc_entries[0].source_files)
            self.assertEqual(6, item_for(output).original_size)

    def test_redump_option_renames_both_and_returns_existing_final_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, dats = self.setup_game(root)
            playlist = folder.parent / "collection.m3u"
            playlist.write_text("My Source Name/My Source Name.cue\n", encoding="utf-8")
            report = ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=True, use_redump_names=True).process_game_dir(folder, [cue])
            target = folder.parent / "Canonical Game (USA)"
            output = target / "Canonical Game (USA).chd"
            self.assertEqual(str(target), report["game_dir"])
            self.assertEqual([str(output)], report["created"])
            self.assertFalse(folder.exists())
            self.assertTrue(output.exists())
            self.assertTrue((target / cue.name).exists())
            self.assertTrue(all(path.exists() for path in bin_files_from_cue(target / cue.name)))
            self.assertEqual(target / "manifest.json", manifest_for(output))
            self.assertEqual("Canonical Game (USA)/Canonical Game (USA).chd\n", playlist.read_text())

    def test_destination_collision_fails_before_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, dats = self.setup_game(root)
            (folder.parent / "Canonical Game (USA)").mkdir()
            tool = NamingTool()
            with patch.object(tool, "createcd") as create:
                with self.assertRaisesRegex(ChdmanError, "destination already exists"):
                    ConvertWorkflow(tool, load_dat_index(dats), use_redump_names=True).process_game_dir(folder, [cue])
            create.assert_not_called()
            self.assertTrue(cue.exists())
            self.assertFalse(list(folder.glob("*.chd")))

    def test_locked_folder_rolls_back_outputs_and_metadata_before_source_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, dats = self.setup_game(root)
            metadata = folder / "manifest.json"
            save_manifest(metadata, Manifest(1, "bin-cue", "Canonical Game (USA)", [ManifestDisc(1, payload_files_from_paths(bin_files_from_cue(cue), "bin"))], "dc"))
            original = metadata.read_bytes()
            playlist = folder.parent / "collection.m3u"
            playlist.write_text("My Source Name/My Source Name.cue\n", encoding="utf-8")
            original_playlist = playlist.read_bytes()
            rename = Path.rename

            def locked(path: Path, target: Path) -> Path:
                if path == folder:
                    raise PermissionError("game folder is locked")
                return rename(path, target)

            with patch.object(Path, "rename", locked), self.assertRaisesRegex(ChdmanError, "rolled back"):
                ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=False, use_redump_names=True).process_game_dir(folder, [cue])
            self.assertEqual(original, metadata.read_bytes())
            self.assertTrue(cue.exists())
            self.assertTrue(all(path.exists() for path in bin_files_from_cue(cue)))
            self.assertFalse(list(folder.glob("*.chd")))
            self.assertEqual(original_playlist, playlist.read_bytes())

    def test_checksum_match_can_update_an_old_source_named_manifest_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, dats = self.setup_game(root)
            metadata = folder / "manifest.json"
            payloads = payload_files_from_paths(bin_files_from_cue(cue), "bin")
            save_manifest(metadata, Manifest(1, "bin-cue", "My Source Name", [ManifestDisc(1, payloads)], "dc"))
            ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=True).process_game_dir(folder, [cue])
            manifest = load_manifest(metadata)
            assert manifest is not None
            self.assertEqual("Canonical Game (USA)", manifest.title)
            self.assertEqual(payloads, manifest.disc_entries[0].payload_files)
            self.assertTrue((folder / "My Source Name.chd").exists())

    def test_preview_reports_both_paths_without_modifying_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, dats = self.setup_game(root)
            with contextlib.redirect_stdout(io.StringIO()):
                report = ConvertWorkflow(Chdman(root / "missing", dry_run=True), load_dat_index(dats), dry_run=True,
                                         keep_sources=False, use_redump_names=True).process_game_dir(folder, [cue])
            self.assertEqual(str(folder.parent / "Canonical Game (USA)"), report["game_dir"])
            self.assertTrue(cue.exists())
            self.assertFalse(list(folder.glob("*.chd")))
            self.assertFalse((folder / "manifest.json").exists())

    def test_redump_names_require_unique_checksums_not_just_a_similar_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cue, _ = self.setup_game(root)
            with self.assertRaisesRegex(ChdmanError, "unique DAT checksum match"):
                ConvertWorkflow(NamingTool(), None, use_redump_names=True).process_game_dir(folder, [cue])
            self.assertTrue(cue.exists())

    def test_multidisc_selected_batch_follows_folder_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Sony - PlayStation 2" / "Old Name"
            folder.mkdir(parents=True)
            dats = root / "dats"
            dats.mkdir()
            files = []
            entries = []
            for disc in (1, 2):
                image = folder / f"Old Name (Disc {disc}).iso"
                payload = f"disc {disc}".encode()
                image.write_bytes(payload)
                files.append(image)
                entries.append(dat_game(f"Canonical (USA) (Disc {disc})", {image.name: payload}))
            (dats / "ps2.dat").write_text("<datafile>" + "".join(entries) + "</datafile>", encoding="utf-8")
            selection = root / "selection.json"
            selection.write_text(json.dumps([str(path) for path in files]), encoding="utf-8")
            with patch("redump_engine.cli.Chdman", return_value=NamingTool()), contextlib.redirect_stdout(io.StringIO()):
                code = run(["--dats", str(dats), "--cuesheets", str(root / "cuesheets"), "convert", "--root", str(root), "--file-list", str(selection), "--use-redump-names", "--keep-sources"])
            self.assertEqual(0, code)
            target = folder.parent / "Canonical (USA)"
            self.assertEqual(2, len(list(target.glob("*.chd"))))
            self.assertTrue(all((target / path.name).exists() for path in files))
            manifest = load_manifest(target / "manifest.json")
            assert manifest is not None
            self.assertEqual([1, 2], [entry.disc for entry in manifest.disc_entries])

    def test_ui_flag_changes_file_and_folder_policy_together(self) -> None:
        for enabled in (False, True):
            command = build_command("convert", Path("Game"), chdman=Path("tool"), dats=Path("dats"), cues=Path("cues"), use_redump_names=enabled)
            self.assertEqual(enabled, "--use-redump-names" in command)
            self.assertEqual(not enabled, "--no-rename-folder" in command)

    def test_disc_folder_merge_without_hard_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second, target, dats = self.setup_separate_discs(root)
            original = {path.name: path.read_bytes() for path in (second, second.with_suffix(".bin"))}
            stamps = {path.name: path.stat().st_mtime_ns for path in (second, second.with_suffix(".bin"))}
            workflow = ConvertWorkflow(NamingTool(), load_dat_index(dats), keep_sources=True, use_redump_names=True)
            workflow.process_game_dir(first.parent, [first])
            with patch("redump_engine.file_transaction.os.link", side_effect=OSError("hard links unsupported")):
                workflow.process_game_dir(second.parent, [second])
            for name, content in original.items():
                self.assertEqual(content, (target / name).read_bytes())
                self.assertEqual(stamps[name], (target / name).stat().st_mtime_ns)
            self.assertFalse(second.parent.exists())
            metadata = load_manifest(target / "manifest.json")
            assert metadata is not None
            self.assertEqual([1, 2], [disc.disc for disc in metadata.disc_entries])
