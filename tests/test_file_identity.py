"""File fingerprints must detect timestamp-preserving edits without reading images."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.actions import VerificationMode
from redump_engine.dat_loader import load_dat_index
from redump_engine.file_identity import file_fingerprint
from redump_engine.verification_cache import VerificationRecords, fingerprint


class FileIdentityTests(unittest.TestCase):
    def test_persisted_image_checks_expire_after_same_size_timestamp_preserving_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dats = root / "dats"
            dats.mkdir()
            image = root / "Game.chd"
            image.write_bytes(b"original image")
            store = VerificationRecords()
            for mode in (VerificationMode.CHD, VerificationMode.IMAGE_INTEGRITY, VerificationMode.IMAGE, VerificationMode.ORIGINALS, VerificationMode.MANIFEST):
                store.remember(image, mode, "Test: Passed", dats)
            reopened = VerificationRecords(json.loads(json.dumps(store.records)))
            self.assertEqual("Test: Passed", reopened.lookup(image, dats))
            before = fingerprint(image)
            stat = image.stat()
            image.write_bytes(b"modified image")
            os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(stat.st_size, image.stat().st_size)
            self.assertEqual(stat.st_mtime_ns, image.stat().st_mtime_ns)
            self.assertNotEqual(before, fingerprint(image))
            self.assertIsNone(reopened.lookup(image, dats))
            self.assertEqual({}, reopened.valid_checks(image, dats))

    def test_fingerprints_are_stable_and_do_not_read_image_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "Game.chd"
            image.write_bytes(b"image")
            with patch.object(Path, "open", side_effect=AssertionError("Image contents must not be read")):
                first = file_fingerprint(image)
                self.assertEqual(first, file_fingerprint(image))
            self.assertEqual(5, len(first))

    @unittest.skipUnless(os.name == "nt", "Windows attribute-query failure")
    def test_unavailable_windows_change_time_does_not_reuse_old_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "Game.chd"
            image.write_bytes(b"image")
            store = VerificationRecords()
            store.remember(image, VerificationMode.CHD, "CHD: Passed", root)
            with patch("redump_engine.file_identity._windows_change_time", side_effect=OSError("query unavailable")):
                self.assertIsNone(fingerprint(image))
                self.assertIsNone(store.lookup(image, root))

    def test_timestamp_preserving_dat_edit_expires_parsed_index_and_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dats = root / "dats"
            dats.mkdir()
            dat = dats / "game.dat"
            original = '<datafile><game name="Game A"><rom name="Game.iso" size="1" sha1="' + 'a' * 40 + '"/></game></datafile>'
            dat.write_text(original, encoding="utf-8")
            first = load_dat_index(dats)
            assert first is not None
            self.assertEqual("Game A", first.entries[0].name)
            image = root / "Game.chd"
            image.write_bytes(b"image")
            store = VerificationRecords()
            store.remember(image, VerificationMode.MANIFEST, "Manifest: Passed", dats)
            stat = dat.stat()
            dat.write_text(original.replace("Game A", "Game B"), encoding="utf-8")
            os.utime(dat, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertIsNone(store.lookup(image, dats))
            second = load_dat_index(dats)
            assert second is not None
            self.assertEqual("Game B", second.entries[0].name)
