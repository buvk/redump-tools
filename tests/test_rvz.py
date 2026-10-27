from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import Chdman
from redump_engine.cli import build_parser, run
from redump_engine.cuesheets import CueRepository
from redump_engine.dolphin_ops import DolphinTool
from redump_engine.library import item_for, manifest_for, scan_library
from redump_engine.metadata import Manifest, ManifestDisc, load_manifest, payload_files_from_paths, save_manifest
from redump_engine.ui_runner import build_command
from redump_engine.updater import selected_assets
from redump_engine.workflow_manifest import ManifestWorkflow
from redump_engine.workflow_rvz import process_rvz


class FakeDolphin(DolphinTool):
    def convert(self, source: Path, destination: Path, *, rvz: bool) -> None:
        destination.write_bytes(zlib.compress(source.read_bytes()) if rvz else zlib.decompress(source.read_bytes()))


class RvzTests(unittest.TestCase):
    def workflow(self, root: Path) -> tuple[FakeDolphin, ManifestWorkflow]:
        executable = root / "DolphinTool.exe"
        executable.touch()
        tool = FakeDolphin(executable)
        return tool, ManifestWorkflow(Chdman(root / "missing.exe"), None, CueRepository(root), dolphin=tool)

    def test_lossless_conversion_restoration_savings_and_source_retention(self) -> None:
        for system in ("Nintendo - GameCube", "Nintendo - Nintendo Wii"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                folder = root / system / "Custom folder"
                folder.mkdir(parents=True)
                source = folder / "Game.iso"
                data = b"original disc" * 500
                source.write_bytes(data)
                tool, manifests = self.workflow(root)
                self.assertEqual("Completed", process_rvz(source, tool, manifests)[0])
                compressed = source.with_suffix(".rvz")
                item = item_for(compressed)
                self.assertEqual("RVZ", item.kind)
                self.assertEqual(len(data), item.original_size)
                self.assertGreater(item.saved_percent or 0, 0)
                self.assertTrue(item.eligible("verify"))
                self.assertTrue(item.eligible("rvz-extract"))
                self.assertFalse(item.eligible("convert"))
                self.assertFalse(item.eligible("xiso"))
                self.assertIn(compressed, [row.path for row in scan_library(root)])
                source.unlink()
                self.assertEqual("Completed", process_rvz(compressed, tool, manifests, extract=True, keep_source=False)[0])
                self.assertEqual(data, source.read_bytes())
                self.assertFalse(compressed.exists())
                metadata_path = manifest_for(source)
                assert metadata_path is not None
                metadata = load_manifest(metadata_path)
                assert metadata is not None
                self.assertEqual("gamecube" if "GameCube" in system else "wii", metadata.system)

    def test_failed_roundtrip_restores_metadata_and_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Game.iso"
            source.write_bytes(b"original")
            tool, manifests = self.workflow(root)
            actual_convert = tool.convert

            def corrupt(source: Path, destination: Path, *, rvz: bool) -> None:
                actual_convert(source, destination, rvz=rvz)
                if not rvz:
                    destination.write_bytes(b"damaged roundtrip")

            with patch.object(tool, "convert", side_effect=corrupt), self.assertRaisesRegex(ValueError, "does not match"):
                process_rvz(source, tool, manifests, keep_source=False)
            self.assertEqual(b"original", source.read_bytes())
            self.assertIsNone(manifest_for(source))
            self.assertFalse(source.with_suffix(".rvz").exists())
            self.assertFalse(list(root.glob(".redump-rvz-*")))

    def test_missing_rvz_manifest_uses_restored_iso_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compressed = root / "Game.rvz"
            original = b"original ISO" * 100
            compressed.write_bytes(zlib.compress(original))
            _, manifests = self.workflow(root)
            self.assertEqual("Completed", manifests.process_file(compressed)[0])
            metadata_path = manifest_for(compressed)
            assert metadata_path is not None
            metadata = load_manifest(metadata_path)
            assert metadata is not None
            self.assertEqual(len(original), metadata.disc_entries[0].payload_files[0]["size"])
            self.assertEqual("Game.iso", metadata.disc_entries[0].payload_files[0]["name"])
            self.assertEqual(["Game.rvz"], metadata.disc_entries[0].source_files)
            self.assertFalse(compressed.with_suffix(".iso").exists())

    def test_stale_manifest_rejected_before_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Game.iso"
            source.write_bytes(b"before")
            metadata = root / "Game.manifest.json"
            save_manifest(metadata, Manifest(1, "iso", "Game", [ManifestDisc(1, payload_files_from_paths([source], "iso"))]))
            before = metadata.read_bytes()
            source.write_bytes(b"modified")
            tool, manifests = self.workflow(root)
            with patch.object(tool, "convert") as convert, self.assertRaisesRegex(ValueError, "does not match"):
                process_rvz(source, tool, manifests)
            convert.assert_not_called()
            self.assertEqual(before, metadata.read_bytes())
            self.assertFalse(source.with_suffix(".rvz").exists())

    def test_preview_collisions_and_console_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Game.iso"
            source.write_bytes(b"original")
            tool, manifests = self.workflow(root)
            with patch.object(tool, "convert") as convert:
                self.assertEqual("Planned", process_rvz(source, tool, manifests, dry_run=True)[0])
                convert.assert_not_called()
            self.assertIsNone(manifest_for(source))
            source.with_suffix(".rvz").write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                process_rvz(source, tool, manifests)
            self.assertEqual(b"existing", source.with_suffix(".rvz").read_bytes())
            ps2 = root / "Sony - PlayStation 2"
            ps2.mkdir()
            other = ps2 / "Game.iso"
            other.write_bytes(b"ps2")
            self.assertFalse(item_for(other).eligible("rvz"))
            self.assertEqual("Skipped", process_rvz(other, tool, manifests)[0])

    def test_cli_and_gui_transport_match_and_keep_sources_by_default(self) -> None:
        for action in ("rvz", "rvz-extract", "manifest"):
            command = build_command(action, Path("games"), chdman=Path("unused"), dats=Path("dats"), cues=Path("cues"),
                                    dolphin_tool=Path("custom tool.exe"), preview=True)
            args = build_parser().parse_args(command[4:])
            self.assertEqual(Path("custom tool.exe"), args.dolphin_tool)
            self.assertTrue(args.dry_run)
            self.assertFalse(getattr(args, "delete_source", False))
        command = build_command("rvz", Path("games"), chdman=Path("unused"), dats=Path("dats"), cues=Path("cues"), keep_sources=False)
        self.assertIn("--delete-source", command)

    def test_cli_selected_conversion_and_restore_use_dolphin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool, _ = self.workflow(root)
            source = root / "Game.iso"
            source.write_bytes(b"original" * 100)
            selection = root / "selection.json"
            selection.write_text(json.dumps([str(source)]), encoding="utf-8")
            with patch("redump_engine.execution.DolphinTool", return_value=tool), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(["rvz", "--root", tmp, "--file-list", str(selection)]))
                self.assertTrue(source.is_file())
                source.unlink()
                selection.write_text(json.dumps([str(source.with_suffix('.rvz'))]), encoding="utf-8")
                self.assertEqual(0, run(["rvz-extract", "--root", tmp, "--file-list", str(selection)]))
            self.assertEqual(b"original" * 100, source.read_bytes())

    def test_nintendo_dat_refresh_has_no_cuesheet_downloads(self) -> None:
        for system in ("gamecube", "wii"):
            self.assertEqual(["dat"], [asset.kind for asset in selected_assets([system])])
            self.assertEqual([], selected_assets([system], include_dats=False))
