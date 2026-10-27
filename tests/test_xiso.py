from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import Chdman
from redump_engine.cli import run
from redump_engine.cuesheets import CueRepository
from redump_engine.library import item_for, manifest_for
from redump_engine.library_presentation import disc_details
from redump_engine.metadata import load_manifest
from redump_engine.ui_runner import build_command
from redump_engine.workflow_manifest import ManifestWorkflow
from redump_engine.workflow_xiso import convert_xiso


class XboxConversionTests(unittest.TestCase):
    def workflow(self, root: Path) -> ManifestWorkflow:
        return ManifestWorkflow(Chdman(root / "no-chdman"), None, CueRepository(root / "no-cues"))

    def fake_tool(self, tool: Path, arguments: list[str]) -> None:
        if arguments[0] == "-r":
            staged = Path(arguments[-1])
            self.assertEqual(b"original disc data", staged.read_bytes())
            (Path(arguments[2]) / staged.name).write_bytes(b"xiso")
            staged.write_bytes(b"tool changed its own input")

    def test_conversion_records_original_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, tool = root / "Game.iso", root / "tool.exe"
            source.write_bytes(b"original disc data")
            tool.touch()
            with patch("redump_engine.workflow_xiso.run_xiso_tool", side_effect=self.fake_tool) as execute:
                status, detail = convert_xiso(source, tool, self.workflow(root))
            self.assertEqual("Completed", status)
            self.assertIn("cannot be reconstructed", detail)
            self.assertEqual(2, execute.call_count)
            self.assertEqual(b"original disc data", source.read_bytes())
            xiso = root / "Game.xiso.iso"
            self.assertEqual(b"xiso", xiso.read_bytes())
            metadata = manifest_for(xiso)
            assert metadata is not None
            manifest = load_manifest(metadata)
            assert manifest is not None
            self.assertEqual(18, manifest.disc_entries[0].payload_files[0]["size"])
            self.assertEqual("Game.iso", manifest.disc_entries[0].payload_files[0]["name"])
            self.assertIn(xiso.name, manifest.disc_entries[0].source_files)
            self.assertEqual(14, item_for(xiso).saved_bytes)
            self.assertIn("cannot be reconstructed", disc_details(item_for(xiso), "Not checked"))
            self.assertFalse(list(root.glob(".redump-xiso-*")))

    def test_failure_preserves_source_and_original_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, tool = root / "Game.iso", root / "tool.exe"
            source.write_bytes(b"original disc data")
            tool.touch()
            with patch("redump_engine.workflow_xiso.run_xiso_tool", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    convert_xiso(source, tool, self.workflow(root), keep_source=False)
            self.assertEqual(b"original disc data", source.read_bytes())
            self.assertIsNotNone(manifest_for(source))
            self.assertFalse((root / "Game.xiso.iso").exists())
            self.assertFalse(list(root.glob(".redump-xiso-*")))

    def test_explicit_deletion_happens_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, tool = root / "Game.iso", root / "tool.exe"
            source.write_bytes(b"original disc data")
            tool.touch()
            with patch("redump_engine.workflow_xiso.run_xiso_tool", side_effect=self.fake_tool):
                convert_xiso(source, tool, self.workflow(root), keep_source=False)
            self.assertFalse(source.exists())
            self.assertIsNotNone(manifest_for(root / "Game.xiso.iso"))

    def test_collision_and_preview_do_not_change_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Game.iso"
            source.write_bytes(b"original disc data")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(["xiso", "--root", str(root), "--game-dir", str(root), "--dry-run"]))
            self.assertIsNone(manifest_for(source))
            target = root / "Game.xiso.iso"
            target.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                convert_xiso(source, root / "missing", self.workflow(root))
            self.assertEqual(b"existing", target.read_bytes())
            self.assertEqual(b"original disc data", source.read_bytes())
            command = build_command("xiso", root, chdman=root, dats=root, cues=root, preview=True)
            self.assertIn("--dry-run", command)
            self.assertNotIn("--delete-source", command)
            self.assertNotIn("--force", command)

    def test_stale_manifest_prevents_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, tool = root / "Game.iso", root / "tool.exe"
            source.write_bytes(b"original disc data")
            tool.touch()
            workflow = self.workflow(root)
            workflow.process_file(source)
            source.write_bytes(b"changed image")
            with patch("redump_engine.workflow_xiso.run_xiso_tool") as execute:
                with self.assertRaisesRegex(ValueError, "differs from its manifest"):
                    convert_xiso(source, tool, workflow)
            execute.assert_not_called()

    def test_already_optimized_image_is_listed_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, tool = root / "Game.iso", root / "tool.exe"
            source.write_bytes(b"original disc data")
            tool.touch()
            with patch("redump_engine.workflow_xiso.run_xiso_tool") as execute:
                self.assertEqual("Completed", convert_xiso(source, tool, self.workflow(root))[0])
            self.assertEqual("-l", execute.call_args_list[-1].args[1][0])
            self.assertEqual(source.read_bytes(), (root / "Game.xiso.iso").read_bytes())

    def test_listing_failure_does_not_publish_or_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, tool = root / "Game.iso", root / "tool.exe"
            source.write_bytes(b"original disc data")
            tool.touch()

            def fail_listing(tool: Path, arguments: list[str]) -> None:
                if arguments[0] == "-l":
                    raise RuntimeError("unreadable XISO")
                self.fake_tool(tool, arguments)

            with patch("redump_engine.workflow_xiso.run_xiso_tool", side_effect=fail_listing):
                with self.assertRaisesRegex(RuntimeError, "unreadable"):
                    convert_xiso(source, tool, self.workflow(root), keep_source=False)
            self.assertTrue(source.exists())
            self.assertFalse((root / "Game.xiso.iso").exists())
