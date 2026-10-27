from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from redump_engine.chd_ops import Chdman, ChdmanError
from redump_engine.events import PREFIX, EventReporter, ToolProgress, decode_event


class ChdProgressTests(unittest.TestCase):
    def test_carriage_return_progress_arrives_before_process_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            release = folder / "continue"
            script = folder / "progress.py"
            script.write_text(
                "import sys,time\nfrom pathlib import Path\n"
                "print('Verifying, 12.5% complete...\\r', end='', file=sys.stderr, flush=True)\n"
                f"release = Path({str(release)!r})\n"
                "deadline = time.monotonic()+5\n"
                "while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                "if not release.exists(): sys.exit(9)\n"
                "print('Verifying, 97.0% complete...\\r', end='', file=sys.stderr, flush=True)\n"
                "print('Verification successful', end='', flush=True)\n", encoding="utf-8")
            updates: list[ToolProgress] = []

            def receive(update: ToolProgress) -> None:
                updates.append(update)
                if update.percent == 12.5:
                    release.touch()

            source = folder / "Game.chd"
            output = Chdman(Path(sys.executable), on_progress=receive)._run(str(script), "-i", str(source))
            self.assertTrue(release.exists())
            self.assertIn("Verification successful", output)
            self.assertEqual([None, 12.5, 97.0, 100.0], [row.percent for row in updates])
            self.assertEqual(["started", "running", "running", "finished"], [row.state for row in updates])
            self.assertTrue(all(row.source == source for row in updates))

    def test_error_preserves_diagnostics_and_does_not_claim_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "failure.py"
            script.write_text("import sys\nprint('Extracting, 23.5% complete...\\r', end='', flush=True)\n"
                              "print('Damaged hunk', file=sys.stderr, flush=True)\nsys.exit(2)\n", encoding="utf-8")
            updates: list[ToolProgress] = []
            with self.assertRaisesRegex(ChdmanError, "Damaged hunk"):
                Chdman(Path(sys.executable), on_progress=updates.append)._run(str(script))
            self.assertEqual("failed", updates[-1].state)
            self.assertEqual(23.5, updates[-1].percent)
            self.assertNotIn(100.0, [row.percent for row in updates])

    def test_progress_event_roundtrip_and_invalid_percentages(self) -> None:
        output = io.StringIO()
        update = ToolProgress("verify", Path("Game.chd").resolve(), 42.5, "Verifying, 42.5% complete...")
        with contextlib.redirect_stdout(output):
            EventReporter(True).tool_progress(update)
        event = decode_event(output.getvalue())
        assert event is not None
        self.assertEqual(update, event.tool)
        for percent in (-1, 101, float("inf"), float("nan"), True, "25"):
            packet = {"version": 1, "kind": "tool-progress", "operation": "verify", "source": None,
                      "percent": percent, "state": "running", "message": "Verifying"}
            self.assertIsNone(decode_event(PREFIX + json.dumps(packet)))
