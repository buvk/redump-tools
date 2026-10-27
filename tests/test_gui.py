from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_hardening import FakeChdman

from redump_engine.cli import build_parser, run
from redump_engine.cuesheets import CueRepository
from redump_engine.events import ResultRecord
from redump_engine.paths import APP_ROOT
from redump_engine.ui_results import export_results_csv
from redump_engine.ui_runner import build_command
from redump_engine.ui_settings import load_last_folder, load_preferences, load_recent_folders, save_last_folder, save_preferences
from redump_engine.workflow_convert import ConvertWorkflow
from redump_engine.workflow_extract import ExtractWorkflow


class DesktopWorkflowTests(unittest.TestCase):
    def test_stop_finishes_current_game_and_does_not_start_next_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("First", "Second"):
                folder = root / name
                folder.mkdir()
                (folder / "Game.chd").touch()
            stop_file = root / "stop"
            tool = FakeChdman()
            def finish_current(path: Path) -> str:
                stop_file.touch()
                return "OK"
            output = io.StringIO()
            with patch.object(tool, "verify", side_effect=finish_current) as verify, patch("redump_engine.cli.Chdman", return_value=tool), contextlib.redirect_stdout(output):
                code = run(["verify", "--root", tmp, "--game-dir", tmp, "--chd-integrity", "--stop-file", str(stop_file)])
            self.assertEqual(0, code)
            self.assertEqual(1, verify.call_count)
            self.assertIn("[batch] stopped", output.getvalue())
            self.assertIn("checked=1 passed=1 failed=0", output.getvalue())

    def test_retry_game_list_checks_only_selected_game_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "First", root / "Second"
            for folder in (first, second):
                folder.mkdir()
                (folder / "Game.chd").touch()
            selection = root / "games.json"
            selection.write_text(json.dumps([str(second), str(second)]), encoding="utf-8")
            tool = FakeChdman()
            with patch.object(tool, "verify", return_value="OK") as verify, patch("redump_engine.cli.Chdman", return_value=tool), contextlib.redirect_stdout(io.StringIO()):
                code = run(["verify", "--root", tmp, "--game-list", str(selection), "--chd-integrity"])
            self.assertEqual(0, code)
            verify.assert_called_once_with(second / "Game.chd")

    def test_settings_and_folder_history_preserve_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            settings = folder / "settings.json"
            values = {"chdman": "Custom tools/chdman.exe", "dats": "Custom DATs", "cuesheets": "Custom cues", "refresh_system": "ps2"}
            save_preferences(values, settings)
            save_last_folder(folder, settings)
            for name, value in values.items():
                expected = str(APP_ROOT / value) if name in {"chdman", "dats", "cuesheets"} else value
                self.assertEqual(expected, load_preferences(settings)[name])
            save_preferences({"refresh_system": "psx"}, settings)
            self.assertEqual(folder.resolve(), load_last_folder(settings))
            self.assertEqual("psx", load_preferences(settings)["refresh_system"])


    def test_export_results_preserves_details_and_unicode(self) -> None:
        rows = [ResultRecord("Game, 日本", "Passed", 'DAT: "Game"\nDisc 2'), ResultRecord("Other game", "Failed", "No DAT match")]
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "results.csv"
            export_results_csv(destination, rows)
            with destination.open(encoding="utf-8-sig", newline="") as handle:
                exported = list(csv.reader(handle))
            self.assertEqual(["Game / item", "System", "Operation", "Status", "Details"], exported[0])
            self.assertEqual([[row.name, "Unknown", "—", row.status, row.details] for row in rows], exported[1:])

    def test_refresh_system_selection_applies_to_dats_and_cuesheets(self) -> None:
        for cuesheets in (False, True):
            command = build_command("update", Path("unused"), chdman=Path("missing.exe"),
                                    dats=Path("dats"), cues=Path("cues"), systems=["ps2"], refresh_cues=cuesheets)
            args = build_parser().parse_args(command[4:])
            self.assertEqual(["ps2"], args.systems)
            self.assertEqual(cuesheets, args.no_dats)
            self.assertEqual(not cuesheets, args.no_cuesheets)

    def test_refresh_cuesheets_leaves_dats_unchanged(self) -> None:
        command = build_command("update", Path("unused"), chdman=Path("missing.exe"),
                                dats=Path("DATs"), cues=Path("My cuesheets"), refresh_cues=True)
        args = build_parser().parse_args(command[4:])
        self.assertEqual("update", args.command)
        self.assertTrue(args.no_dats)
        self.assertFalse(args.no_cuesheets)
        self.assertEqual(Path("My cuesheets"), args.cuesheets)
        self.assertNotIn("--game-dir", command)

    def test_joint_reference_refresh_is_the_default(self) -> None:
        command = build_command("update", Path("unused"), chdman=Path("unused"), dats=Path("DATs"), cues=Path("Cues"))
        args = build_parser().parse_args(command[4:])
        self.assertFalse(args.no_dats)
        self.assertFalse(args.no_cuesheets)


    def test_recent_folders_are_unique_and_most_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings.json"
            folders = [root / name for name in ("PlayStation", "PlayStation 2")]
            for folder in folders:
                folder.mkdir()
                save_last_folder(folder, settings)
            save_last_folder(folders[0], settings)
            self.assertEqual([str(folder.resolve()) for folder in folders], load_recent_folders(settings))

    def test_last_folder_is_restored_and_bad_settings_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings.json"
            folder = root / "Game folder"
            folder.mkdir()
            self.assertEqual(APP_ROOT, load_last_folder(settings))
            save_last_folder(folder, settings)
            self.assertEqual(folder.resolve(), load_last_folder(settings))
            self.assertFalse(settings.with_suffix(".tmp").exists())
            save_last_folder(root / "missing", settings)
            self.assertEqual(folder.resolve(), load_last_folder(settings))
            folder.rmdir()
            self.assertEqual(APP_ROOT, load_last_folder(settings))
            for contents in ("broken JSON", "[]", '{"last_folder": 123}', '{"last_folder": ""}'):
                settings.write_text(contents, encoding="utf-8")
                self.assertEqual(APP_ROOT, load_last_folder(settings))

    def test_refresh_dats_command_downloads_only_dats_without_game_folder(self) -> None:
        command = build_command("update", Path("unused game folder"), chdman=Path("missing.exe"),
                                dats=Path("My DAT folder"), cues=Path("cues"), refresh_cues=False)
        args = build_parser().parse_args(command[4:])
        self.assertEqual("update", args.command)
        self.assertEqual(Path("My DAT folder"), args.dats)
        self.assertTrue(args.no_cuesheets)
        self.assertFalse(args.no_dats)
        self.assertNotIn("--game-dir", command)
        self.assertNotIn("--rehash", command)

    def test_chd_verification_command_does_not_request_original_payloads(self) -> None:
        command = build_command("verify", Path("game"), chdman=Path("chdman.exe"),
                                dats=Path("dats"), cues=Path("cues"), check_chds=True)
        args = build_parser().parse_args(command[4:])
        self.assertTrue(args.chd_integrity)
        self.assertFalse(args.rehash)

    def test_chd_integrity_needs_neither_original_images_nor_manifest_nor_dats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "Game.chd").write_bytes(b"compressed")
            for verify_ok, expected_exit in ((True, 0), (False, 2)):
                output = io.StringIO()
                with (
                    patch("redump_engine.cli.Chdman", return_value=FakeChdman(verify_ok=verify_ok)),
                    patch("redump_engine.cli.load_dat_index") as dat_loader,
                    contextlib.redirect_stdout(output),
                ):
                    code = run(["verify", "--root", tmp, "--game-dir", tmp, "--chd-integrity"])
                self.assertEqual(expected_exit, code)
                dat_loader.assert_not_called()
                self.assertIn("checked=1", output.getvalue())
                self.assertIn("passed=1" if verify_ok else "failed=1", output.getvalue())
                self.assertTrue((folder / "Game.chd").is_file())

    def test_chd_integrity_continues_after_a_bad_chd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("Bad.chd", "Good.chd"):
                (folder / name).touch()
            tool = FakeChdman()
            output = io.StringIO()
            with (
                patch.object(tool, "verify", side_effect=[RuntimeError("bad CHD"), "OK"]) as verify,
                patch("redump_engine.cli.Chdman", return_value=tool),
                contextlib.redirect_stdout(output),
            ):
                code = run(["verify", "--root", tmp, "--game-dir", tmp, "--chd-integrity"])
            self.assertEqual(2, code)
            self.assertEqual(2, verify.call_count)
            self.assertIn("checked=2 passed=1 failed=1", output.getvalue())

    def test_ui_commands_use_safe_defaults_and_preserve_paths_with_spaces(self) -> None:
        for operation in ("extract", "convert", "verify"):
            command = build_command(operation, Path("Game folder"), chdman=Path("tools/chdman.exe"),
                                    dats=Path("DAT folder"), cues=Path("Cue folder"))
            args = build_parser().parse_args(command[4:])
            self.assertEqual(Path("Game folder"), args.game_dir)
            if operation == "verify":
                self.assertFalse(args.rehash)
                self.assertFalse(args.chd_integrity)
            else:
                self.assertTrue(args.keep_sources)
                self.assertTrue(args.no_rename_folder)
                self.assertFalse(args.force)

    def test_preview_and_explicit_destructive_options(self) -> None:
        command = build_command("extract", Path("game"), chdman=Path("chdman.exe"),
                                dats=Path("dats"), cues=Path("cues"), preview=True,
                                keep_sources=False, force=True)
        args = build_parser().parse_args(command[4:])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.force)
        self.assertFalse(args.keep_sources)

    def test_keep_sources_round_trip_retains_images_and_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Original folder"
            folder.mkdir()
            iso = folder / "Game.iso"
            iso.write_bytes(b"disc payload")
            convert = ConvertWorkflow(FakeChdman(), None, keep_sources=True, rename_folder=False)
            report = convert.process_game_dir(folder)
            self.assertEqual("converted", report["status"])
            self.assertTrue(iso.is_file())
            chd = next(folder.glob("*.chd"))
            self.assertEqual(iso.read_bytes(), chd.read_bytes())
            extract = ExtractWorkflow(FakeChdman(force=True), None, CueRepository(Path(tmp) / "cues"),
                                      keep_sources=True, rename_folder=False)
            results = extract.process_game_dir(folder)
            self.assertTrue(chd.is_file())
            self.assertTrue(folder.is_dir())
            self.assertEqual([], results[0].removed_files)
            self.assertTrue(all(path.is_file() for path in results[0].extracted_files))
