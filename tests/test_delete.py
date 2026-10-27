from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from redump_engine.cli import run
from redump_engine.events import decode_event
from redump_engine.library import item_for
from redump_engine.workflow_delete import deletion_plan


class DeleteSelectionTests(unittest.TestCase):
    def test_plan_preserves_unselected_metadata_and_other_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two = root / "One.chd", root / "Two.iso"
            one.write_bytes(b"chd")
            two.write_bytes(b"iso")
            metadata = root / "manifest.json"
            metadata.write_text('{}', encoding="utf-8")
            files, retained = deletion_plan([item_for(one)], root)
            self.assertEqual([one], files)
            self.assertEqual([], retained)
            selection = root / "selection.json"
            selection.write_text(json.dumps([str(path) for path in files]), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(["delete", "--root", str(root), "--file-list", str(selection), "--confirm-delete"]))
            self.assertFalse(one.exists())
            self.assertTrue(two.exists())
            self.assertTrue(metadata.exists())
            self.assertTrue(root.is_dir())

    def test_cli_dry_run_preserves_files_and_reports_planned_results(self):
        for before_command in (True, False):
            with self.subTest(before_command=before_command), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                image = root / "Game.iso"
                image.write_bytes(b"original image")
                selection = root / "selection.json"
                selection.write_text(json.dumps([str(image)]), encoding="utf-8")
                args = ["delete", "--root", str(root), "--file-list", str(selection), "--confirm-delete", "--events"]
                if before_command:
                    args.insert(0, "--dry-run")
                else:
                    args.append("--dry-run")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(0, run(args))
                self.assertEqual(b"original image", image.read_bytes())
                events = [event for line in output.getvalue().splitlines() if (event := decode_event(line)) is not None]
                results = [event.result for event in events if event.result is not None]
                self.assertEqual(["Planned"], [result.status for result in results])
                self.assertIn("no files changed", results[0].details)

    def test_shared_tracks_retained_unless_all_cues_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two = root / "One.cue", root / "Two.cue"
            for cue in (one, two):
                cue.write_text('FILE "track.bin" BINARY\n TRACK 01 MODE1/2352\n', encoding="utf-8")
            track = root / "track.bin"
            track.write_bytes(b"shared")
            files, retained = deletion_plan([item_for(one)], root)
            self.assertEqual([one], files)
            self.assertEqual([track], retained)
            files, retained = deletion_plan([item_for(one), item_for(two)], root)
            self.assertEqual({one, two, track}, set(files))
            self.assertEqual([], retained)
            self.assertEqual(3, len(files))

    def test_cli_requires_explicit_confirmation_and_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Game.iso"
            path.write_bytes(b"iso")
            selection = root / "selection.json"
            selection.write_text(json.dumps([str(path)]), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run(["delete", "--root", str(root), "--file-list", str(selection)])
            self.assertTrue(path.exists())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run(["delete", "--root", str(root), "--confirm-delete"])
            self.assertTrue(path.exists())

    def test_plan_refuses_files_outside_the_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.iso"
            outside.touch()
            library = root / "Library"
            library.mkdir()
            with self.assertRaises(ValueError):
                deletion_plan([item_for(outside)], library)
