"""Atomic metadata publication must isolate aliases and preserve old files on error."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.file_io import write_metadata
from redump_engine.file_transaction import FileTransaction


class MetadataIoTests(unittest.TestCase):
    def test_writes_do_not_change_hard_linked_metadata(self):
        for extension in (".json", ".cue", ".m3u"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                selected, backup = root / ("selected" + extension), root / ("backup" + extension)
                selected.write_bytes(b"original metadata")
                os.link(selected, backup)
                self.assertEqual(len(b"updated metadata"), write_metadata(selected, b"updated metadata"))
                self.assertEqual(b"updated metadata", selected.read_bytes())
                self.assertEqual(b"original metadata", backup.read_bytes())
                self.assertFalse(selected.samefile(backup))
                self.assertEqual({selected, backup}, set(root.iterdir()))

    def test_failed_publication_keeps_previous_metadata_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected, backup = root / "manifest.json", root / "backup.json"
            selected.write_bytes(b"original metadata")
            os.link(selected, backup)
            replace = Path.replace
            def fail_selected(path, target):
                if target == selected:
                    raise PermissionError("metadata locked")
                return replace(path, target)
            with patch.object(Path, "replace", fail_selected):
                with self.assertRaisesRegex(PermissionError, "metadata locked"):
                    write_metadata(selected, b"updated metadata")
            self.assertEqual(b"original metadata", selected.read_bytes())
            self.assertEqual(b"original metadata", backup.read_bytes())
            self.assertTrue(selected.samefile(backup))
            self.assertEqual({selected, backup}, set(root.iterdir()))

    def test_rollback_does_not_overwrite_edits_to_an_unselected_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected, backup = root / "manifest.json", root / "backup.json"
            selected.write_bytes(b"original metadata")
            os.link(selected, backup)
            with self.assertRaisesRegex(OSError, "later failure"):
                with FileTransaction() as transaction:
                    transaction.watch_metadata([selected])
                    write_metadata(selected, b"updated metadata")
                    backup.write_bytes(b"independent backup edit")
                    raise OSError("later failure")
            self.assertEqual(b"original metadata", selected.read_bytes())
            self.assertEqual(b"independent backup edit", backup.read_bytes())
            self.assertEqual({selected, backup}, set(root.iterdir()))
