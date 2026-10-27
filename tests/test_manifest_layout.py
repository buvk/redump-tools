from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from redump_engine.actions import ACTIONS
from redump_engine.chd_ops import Chdman
from redump_engine.cuesheets import CueRepository
from redump_engine.events import EventReporter
from redump_engine.execution import ExecutionOptions, WorkflowExecutor, run_batch
from redump_engine.manifest_layout import manifest_layout_warnings
from redump_engine.metadata import Manifest, ManifestDisc, load_dat_index, payload_files_from_paths, save_manifest
from redump_engine.ui_results import completion_summary, result_summary


class ManifestLayoutTests(unittest.TestCase):
    def setup_manifest(self, root: Path, correct: bool) -> tuple[Path, Path, Manifest]:
        title = "Game (USA) (En,Fr,De)"
        game = root / "Nintendo - GameCube" / (title if correct else "Game (USA)")
        game.mkdir(parents=True)
        original = root / "original.iso"
        original.write_bytes(b"original image bytes")
        payload = payload_files_from_paths([original], "iso")
        image = game / ((title if correct else "Game (USA)") + ".rvz")
        image.write_bytes(b"compressed placeholder")
        manifest = Manifest(1, "iso", title, [ManifestDisc(1, payload, [image.name])], "gamecube")
        metadata = game / ("manifest.json" if correct else title + ".manifest.json")
        save_manifest(metadata, manifest)
        dats = root / "dats"
        dats.mkdir()
        (dats / "gc.dat").write_text('<datafile><game name="' + title + '"><rom name="' + title + '.iso" '
                                     f'size="{payload[0]["size"]}" sha1="{payload[0]["sha1"]}" crc="{payload[0]["crc32"]}"/>'
                                     '</game></datafile>', encoding="utf-8")
        return metadata, dats, manifest

    def test_wrong_folder_image_and_manifest_names_are_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, dats, manifest = self.setup_manifest(root, False)
            index = load_dat_index(dats)
            warnings = manifest_layout_warnings(path, manifest, index)
            self.assertEqual(3, len(warnings))
            self.assertTrue(any("Game folder name:" in text for text in warnings))
            self.assertTrue(any("expected 'manifest.json'" in text for text in warnings))
            self.assertTrue(any("Game (USA) (En,Fr,De).rvz" in text for text in warnings))
            executor = WorkflowExecutor(ACTIONS["verify"], Chdman(root / "unused"), index, CueRepository(root),
                                        ExecutionOptions(), EventReporter(), selected=True)
            outcome = executor.execute(path)
            self.assertEqual("Warning", outcome.records[0].status)
            self.assertIn("Manifest matches Redump DAT", outcome.records[0].details)
            self.assertEqual("Recorded checksums match Redump DAT; naming/layout needs attention.", result_summary(outcome.records[0]))
            self.assertIsNotNone(outcome.verification)
            assert outcome.verification is not None
            self.assertEqual("passed", outcome.verification.status)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, run_batch([path], executor, EventReporter()))
            self.assertIn("checked=1 passed=0 failed=0 skipped=0 warnings=1", output.getvalue())
            text, color, _ = completion_summary({"checked": 1, "warnings": 1}, outcome.records,
                                                 preview=False, exit_code=0, stopped=False, elapsed=1)
            self.assertIn("Finished with warnings", text)
            self.assertEqual("#895600", color)

    def test_canonical_layout_passes_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path, dats, manifest = self.setup_manifest(Path(tmp), True)
            self.assertEqual([], manifest_layout_warnings(path, manifest, load_dat_index(dats)))
