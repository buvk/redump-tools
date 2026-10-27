from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_hardening import FakeChdman

from redump_engine.chd_ops import ChdmanError
from redump_engine.file_io import write_metadata
from redump_engine.file_transaction import FileTransaction
from redump_engine.workflow_convert import ConvertWorkflow


class FileTransactionTests(unittest.TestCase):
    def test_rollback_after_folder_rename_restores_metadata_and_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, renamed = root / "Game", root / "New name"
            folder.mkdir()
            source, metadata = folder / "Game.iso", folder / "manifest.json"
            source.write_bytes(b"source")
            metadata.write_bytes(b"original metadata")
            target_dir = folder / "Disc"
            with self.assertRaisesRegex(OSError, "later failure"):
                with FileTransaction() as transaction:
                    transaction.watch_metadata([metadata, target_dir / "new.json"])
                    transaction.mkdir(target_dir)
                    transaction.transfer(source, target_dir / source.name)
                    metadata.write_bytes(b"changed")
                    (target_dir / "new.json").write_bytes(b"new metadata")
                    transaction.rename_folder(folder, renamed)
                    raise OSError("later failure")
            self.assertEqual(b"source", source.read_bytes())
            self.assertEqual(b"original metadata", metadata.read_bytes())
            self.assertFalse(target_dir.exists())
            self.assertFalse(renamed.exists())

    def test_partial_copy_is_removed_and_shared_source_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "source.bin", Path(tmp) / "target.bin"
            source.write_bytes(b"source")

            def fail_copy(incoming, outgoing):
                outgoing.write(b"partial")
                raise OSError("disk full")

            with patch("redump_engine.file_transaction.shutil.copyfileobj", side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, "disk full"):
                    with FileTransaction() as transaction:
                        transaction.transfer(source, target, copy=True)
            self.assertEqual(b"source", source.read_bytes())
            self.assertFalse(target.exists())

    def test_collision_keeps_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "source", Path(tmp) / "target"
            source.write_bytes(b"source")
            target.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                with FileTransaction() as transaction:
                    transaction.transfer(source, target)
            self.assertEqual(b"source", source.read_bytes())
            self.assertEqual(b"existing", target.read_bytes())

    def test_one_rollback_failure_does_not_stop_other_restorations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one, two = root / "one", root / "two"
            one.write_bytes(b"original one")
            two.write_bytes(b"original two")
            real_write = Path.write_bytes

            def fail_one(path, content):
                if path == one:
                    raise PermissionError("one locked")
                return write_metadata(path, content)

            with self.assertRaises(ExceptionGroup) as raised:
                with patch("redump_engine.file_transaction.write_metadata", side_effect=fail_one):
                    with FileTransaction() as transaction:
                        transaction.watch_metadata([two, one])
                        real_write(one, b"changed")
                        real_write(two, b"changed")
                        raise OSError("operation failed")
            self.assertEqual(b"original two", two.read_bytes())
            self.assertIsInstance(raised.exception.exceptions[0], OSError)

    def test_chd_overwrite_isolates_linked_inputs_and_unselected_aliases(self):
        class CompressingTool(FakeChdman):
            def createdvd(self, input_image: Path, output_chd: Path) -> str:
                self._force_flags(output_chd)
                output_chd.write_bytes(b"new compressed image")
                return ""
        for linked_input in (False, True):
            for fail in (False, True):
                with self.subTest(linked_input=linked_input, fail=fail), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    folder = root / "Game"
                    folder.mkdir()
                    source, output, alias = folder / "Game.iso", folder / "Game.chd", root / "Backup.chd"
                    source.write_bytes(b"original ISO bytes")
                    if linked_input:
                        os.link(source, output)
                    else:
                        output.write_bytes(b"previous CHD bytes")
                    os.link(output, alias)
                    previous = output.read_bytes()
                    tool = CompressingTool(force=True)
                    workflow = ConvertWorkflow(tool, None, keep_sources=True, rename_folder=False)
                    if fail:
                        with patch.object(tool, "verify", side_effect=ChdmanError("verification failed")):
                            with self.assertRaisesRegex(ChdmanError, "verification failed"):
                                workflow.process_game_dir(folder, [source])
                    else:
                        workflow.process_game_dir(folder, [source])
                    self.assertEqual(b"original ISO bytes", source.read_bytes())
                    self.assertEqual(previous, alias.read_bytes())
                    self.assertEqual(previous if fail else b"new compressed image", output.read_bytes())
                    self.assertFalse(list(root.rglob(".rollback_*")))

    def test_failed_force_conversion_restores_existing_chd(self) -> None:
        class FailingVerify(FakeChdman):
            def verify(self, input_chd: Path) -> str:
                raise ChdmanError("verification failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "Game.iso", root / "Game.chd"
            source.write_bytes(b"original source")
            output.write_bytes(b"previous CHD")
            with self.assertRaisesRegex(ChdmanError, "verification failed"):
                ConvertWorkflow(FailingVerify(force=True), None).process_game_dir(root)
            self.assertEqual(b"original source", source.read_bytes())
            self.assertEqual(b"previous CHD", output.read_bytes())
            self.assertEqual({source, output}, set(root.iterdir()))

    def test_success_keeps_output_and_removes_disk_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Game.chd"
            output.write_bytes(b"previous")
            with FileTransaction() as transaction:
                transaction.watch_output(output)
                output.write_bytes(b"replacement")
            self.assertEqual(b"replacement", output.read_bytes())
            self.assertEqual([output], list(Path(tmp).iterdir()))

    def test_output_backup_follows_folder_rename_on_success_and_failure(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                folder, target = Path(tmp) / "Game", Path(tmp) / "Renamed"
                folder.mkdir()
                output = folder / "Game.chd"
                output.write_bytes(b"previous")

                def change(output: Path = output, folder: Path = folder,
                           target: Path = target, fail: bool = fail) -> None:
                    with FileTransaction() as transaction:
                        transaction.watch_output(output)
                        output.write_bytes(b"replacement")
                        transaction.rename_folder(folder, target)
                        if fail:
                            raise OSError("later failure")

                if fail:
                    with self.assertRaisesRegex(OSError, "later failure"):
                        change()
                else:
                    change()
                final = folder if fail else target
                self.assertEqual(b"previous" if fail else b"replacement", (final / output.name).read_bytes())
                self.assertEqual([final / output.name], list(final.iterdir()))

    def test_rollback_conflict_preserves_both_files_and_reports_recovery_path(self):
        for hardlinks in (True, False):
            with self.subTest(hardlinks=hardlinks), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, target = root / "source.iso", root / "moved.iso"
                source.write_bytes(b"original bytes")
                with self.assertRaises(ExceptionGroup) as raised:
                    with FileTransaction() as transaction:
                        if hardlinks:
                            transaction.transfer(source, target)
                        else:
                            with patch("redump_engine.file_transaction.os.link", side_effect=OSError("unsupported")):
                                transaction.transfer(source, target)
                        source.write_bytes(b"new occupant")
                        raise OSError("later failure")
                self.assertEqual(b"original bytes", target.read_bytes())
                self.assertEqual(b"new occupant", source.read_bytes())
                self.assertIn(str(target), str(raised.exception))
