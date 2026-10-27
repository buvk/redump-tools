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

from redump_engine.cli import run
from redump_engine.events import PREFIX, EventReporter, ResultRecord, decode_event
from redump_engine.ui_results import failed_folders, visible_result_indices
from redump_engine.ui_runner import DesktopRunner, build_command
from redump_engine.updater import UpdateResult


class StructuredEventTests(unittest.TestCase):
    def test_summaries_and_suggested_actions_survive_event_transport(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            EventReporter(True).result("Game", "Warning", "Diagnostic wording can change.",
                                       summary="Naming needs attention", suggested_action="repair-layout")
        event = decode_event(output.getvalue())
        assert event is not None and event.result is not None
        self.assertEqual("Naming needs attention", event.result.summary)
        self.assertEqual("repair-layout", event.result.suggested_action)
        from redump_engine.ui_results import result_summary
        self.assertEqual("Naming needs attention", result_summary(event.result))
        invalid = json.loads(output.getvalue()[len(PREFIX):])
        invalid["suggested_action"] = ["repair-layout"]
        self.assertIsNone(decode_event(PREFIX + json.dumps(invalid)))

    def test_results_preserve_multiline_text_and_explicit_folder(self) -> None:
        output = io.StringIO()
        folder = Path("Game folder").resolve()
        with contextlib.redirect_stdout(output):
            EventReporter(True).result("Game: 日本", "Failed", "First line\nSecond: line", folder)
        self.assertEqual(1, len(output.getvalue().splitlines()))
        event = decode_event(output.getvalue())
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(ResultRecord("Game: 日本", "Failed", "First line\nSecond: line", folder), event.result)
        self.assertIsNone(decode_event("[verify] FAIL Game: human-readable wording"))

    def test_invalid_messages_are_ignored(self) -> None:
        for data in ([], {}, {"version": 2, "kind": "stopped"}, {"version": 1, "kind": "unknown"},
                     {"version": 1, "kind": "progress", "current": 0, "total": 2, "folder": "game"},
                     {"version": 1, "kind": "summary", "counts": {"passed": -1}},
                     {"version": 1, "kind": "result", "name": "Game", "status": "Passed", "details": None}):
            self.assertIsNone(decode_event(PREFIX + json.dumps(data)))
        self.assertIsNone(decode_event(PREFIX + "broken JSON"))

    def test_cli_emits_explicit_failure_paths_progress_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "Game.chd").touch()
            output = io.StringIO()
            with patch("redump_engine.cli.Chdman", return_value=FakeChdman(verify_ok=False)), contextlib.redirect_stdout(output):
                code = run(["verify", "--root", tmp, "--game-dir", tmp, "--chd-integrity", "--events"])
            events = [event for line in output.getvalue().splitlines() if (event := decode_event(line)) is not None]
            self.assertEqual(2, code)
            self.assertEqual(["progress", "result", "summary"], [event.kind for event in events])
            self.assertEqual((1, 1, folder), (events[0].current, events[0].total, events[0].folder))
            assert events[1].result is not None
            self.assertEqual(folder, events[1].result.folder)
            self.assertEqual("Failed", events[1].result.status)
            self.assertEqual({"checked": 1, "passed": 0, "failed": 1}, events[2].counts)

    def test_all_game_operations_emit_records_and_summary(self) -> None:
        for operation in ("extract", "convert", "verify"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / ("Game.chd" if operation == "extract" else "Game.iso")).write_bytes(b"payload")
                args = [operation, "--root", tmp, "--game-dir", tmp, "--events"]
                if operation != "verify":
                    args += ["--keep-sources", "--no-rename-folder"]
                output = io.StringIO()
                with patch("redump_engine.cli.Chdman", return_value=FakeChdman()), contextlib.redirect_stdout(output):
                    self.assertEqual(0, run(args))
                events = [event for line in output.getvalue().splitlines() if (event := decode_event(line)) is not None]
                self.assertEqual("summary", events[-1].kind)
                record = next(event.result for event in events if event.result is not None)
                self.assertEqual(folder, record.folder)
                self.assertEqual("Skipped" if operation == "verify" else "Completed", record.status)

    def test_reference_results_include_actual_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "cues" / "ps2.zip"
            output = io.StringIO()
            result = UpdateResult("ps2", "cue", "https://example.test/cues", destination, True)
            with patch("redump_engine.cli.update_assets", return_value=[result]), contextlib.redirect_stdout(output):
                self.assertEqual(0, run(["update", "--root", tmp, "--no-dats", "--events"]))
            event = next(event for line in output.getvalue().splitlines() if (event := decode_event(line)) is not None)
            assert event.result is not None
            self.assertEqual(destination, event.result.reference)
            self.assertEqual("Updated", event.result.status)

    def test_result_folder_stays_attached_when_filtered_sorted_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            rows = [ResultRecord("Zebra", "Failed", "No match", folder), ResultRecord("Alpha", "Passed", "Matched", folder)]
            self.assertEqual([1, 0], visible_result_indices(rows, "All results", "", 0, False))
            self.assertEqual([0], visible_result_indices(rows, "Failed", "zebra", 0, False))
            self.assertEqual([folder], failed_folders(rows))
            self.assertEqual(folder, rows[0].folder)

    def test_details_sort_uses_visible_summaries_and_game_name_for_ties(self) -> None:
        rows = [ResultRecord("Zebra", "Warning", "Manifest matches Redump DAT: Alpha. Naming/layout needs attention: wrong folder",
                             summary="Recorded checksums match Redump DAT; naming/layout needs attention."),
                ResultRecord("Beta", "Passed", "Manifest matches Redump DAT: Zulu.", summary="Manifest matches Redump DAT."),
                ResultRecord("Alpha", "Warning", "Manifest matches Redump DAT: Zulu. Naming/layout needs attention: wrong filename",
                             summary="Recorded checksums match Redump DAT; naming/layout needs attention."),
                ResultRecord("manifest.json", "Passed", "Manifest matches Redump DAT: Beta.", Path("collection/Alpha"),
                             summary="Manifest matches Redump DAT.")]
        self.assertEqual([3, 1, 2, 0], visible_result_indices(rows, "All results", "", 4, False))
        self.assertEqual([0, 2, 1, 3], visible_result_indices(rows, "All results", "", 4, True))
        self.assertEqual([2, 0], visible_result_indices(rows, "Warnings", "", 4, False))
        self.assertEqual([2, 3, 1, 0], visible_result_indices(rows, "All results", "", 0, False))

    def test_runner_repeated_retry_replaces_old_selection_and_cleans_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("redump_engine.ui_runner.threading.Thread") as thread:
            folder = Path(tmp)
            command = build_command("verify", folder, chdman=folder / "missing.exe", dats=folder, cues=folder)
            runner = DesktopRunner(queue.Queue())
            try:
                runner.start(command)
                runner.request_stop()
                assert runner.stop_file is not None
                self.assertTrue(runner.stop_file.exists())
                runner.cleanup()
                for _ in range(2):
                    runner.start(runner.last_command, [folder])
                    self.assertEqual("verify", runner.operation)
                    selection = Path(runner.last_command[runner.last_command.index("--game-list") + 1])
                    self.assertEqual([str(folder)], json.loads(selection.read_text(encoding="utf-8")))
                    self.assertNotIn("--game-dir", runner.last_command)
                    runner.cleanup()
                    self.assertFalse(selection.exists())
                self.assertEqual(3, thread.call_count)
            finally:
                runner.cleanup()


class VerificationEvidenceTransportTests(unittest.TestCase):
    def test_checked_fingerprints_survive_event_transport(self):
        evidence = {str(Path("Game.iso").resolve()): {"file": [100, 200, 300], "evidence": 1}}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            EventReporter(True).result("Game", "Passed", "Matched", verified_inputs=evidence)
        event = decode_event(output.getvalue())
        assert event is not None and event.result is not None
        self.assertEqual(evidence, event.result.verified_inputs)


class ResultContextTests(unittest.TestCase):
    def test_system_and_operation_survive_transport_and_search(self):
        from redump_engine.ui_results import result_columns
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            EventReporter(True).result("Game", "Passed", "Matched", system="xbox", operation="verify")
        event = decode_event(output.getvalue())
        assert event is not None and event.result is not None
        self.assertEqual("xbox", event.result.system)
        self.assertEqual("verify", event.result.operation)
        self.assertEqual("Xbox", result_columns(event.result)[1])
        self.assertEqual("Verify manifest against Redump DAT", result_columns(event.result)[2])
        self.assertEqual([0], visible_result_indices([event.result], "All results", "Xbox", 0, False))
        self.assertEqual([0], visible_result_indices([event.result], "All results", "Verify manifest", 0, False))
