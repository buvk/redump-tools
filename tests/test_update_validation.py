"""Downloads must be usable before replacing a previously working archive."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from redump_engine.dat_loader import load_dat_index
from redump_engine.updater import update_assets

GOOD_DAT = '<datafile><header><name>Sony - PlayStation</name></header><game name="Good"><rom name="Good.bin" size="1" sha1="' + 'a' * 40 + '" crc="12345678"/></game></datafile>'
GOOD_CUE = 'FILE "Good.bin" BINARY\n TRACK 01 MODE1/2352\n INDEX 01 00:00:00\n'


def archive_bytes(members):
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return data.getvalue()


class DownloadValidationTests(unittest.TestCase):
    def test_invalid_dat_contents_keep_the_working_archive(self):
        candidates = {
            "empty archive": {},
            "no DAT": {"error.html": "service unavailable"},
            "bad XML": {"game.dat": "<datafile>"},
            "no games": {"game.dat": "<datafile/>"},
            "bad size": {"game.dat": GOOD_DAT.replace('size="1"', 'size="invalid"')},
            "negative size": {"game.dat": GOOD_DAT.replace('size="1"', 'size="-1"')},
            "missing size": {"game.dat": GOOD_DAT.replace(' size="1"', '')},
            "bad SHA1": {"game.dat": GOOD_DAT.replace('a' * 40, 'invalid')},
            "bad CRC": {"game.dat": GOOD_DAT.replace('12345678', 'invalid')},
            "wrong system": {"game.dat": GOOD_DAT.replace('Sony - PlayStation', 'Sony - PlayStation 2')},
            "mixed valid and invalid": {"good.dat": GOOD_DAT, "bad.dat": "<datafile>"},
        }
        for name, members in candidates.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                existing = root / "redump_psx_dat.zip"
                original = archive_bytes({"good.dat": GOOD_DAT})
                existing.write_bytes(original)
                with patch("redump_engine.updater.urlopen", return_value=io.BytesIO(archive_bytes(members))):
                    results = update_assets(dats_dir=root, cuesheets_dir=root / "cues", systems=["psx"], include_cuesheets=False)
                self.assertFalse(results[0].changed)
                self.assertIsNotNone(results[0].error)
                self.assertEqual(original, existing.read_bytes())
                self.assertEqual({existing}, set(root.iterdir()))
                index = load_dat_index(root)
                assert index is not None
                self.assertEqual(["Good"], [entry.name for entry in index.entries])

    def test_valid_nested_dat_and_cuesheet_archives_are_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dats, cues = root / "dats", root / "cues"
            dat_zip = archive_bytes({"nested/PlayStation.DAT": GOOD_DAT})
            cue_zip = archive_bytes({"nested/Good.cue": GOOD_CUE})
            with patch("redump_engine.updater.urlopen", side_effect=[io.BytesIO(dat_zip), io.BytesIO(cue_zip)]):
                results = update_assets(dats_dir=dats, cuesheets_dir=cues, systems=["psx"])
            self.assertTrue(all(result.changed and result.error is None for result in results))
            self.assertEqual(dat_zip, (dats / "redump_psx_dat.zip").read_bytes())
            self.assertEqual(cue_zip, (cues / "redump_psx_cuesheets.zip").read_bytes())
            index = load_dat_index(dats)
            assert index is not None
            self.assertEqual(["Good"], [entry.name for entry in index.entries])

    def test_invalid_dat_does_not_prevent_other_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dats, cues = root / "dats", root / "cues"
            dats.mkdir()
            existing = dats / "redump_psx_dat.zip"
            original = archive_bytes({"good.dat": GOOD_DAT})
            existing.write_bytes(original)
            valid_cues = archive_bytes({"Good.cue": GOOD_CUE})
            with patch("redump_engine.updater.urlopen", side_effect=[io.BytesIO(archive_bytes({})), io.BytesIO(valid_cues)]):
                results = update_assets(dats_dir=dats, cuesheets_dir=cues, systems=["psx"])
            self.assertIsNotNone(results[0].error)
            self.assertFalse(results[0].changed)
            self.assertTrue(results[1].changed)
            self.assertEqual(original, existing.read_bytes())
            self.assertEqual(valid_cues, (cues / "redump_psx_cuesheets.zip").read_bytes())

    def test_invalid_cuesheets_keep_the_previous_archive(self):
        for members in ({}, {"error.txt": "unavailable"}, {"Good.cue": "not a cuesheet"}):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                existing = root / "redump_psx_cuesheets.zip"
                original = archive_bytes({"Good.cue": GOOD_CUE})
                existing.write_bytes(original)
                with patch("redump_engine.updater.urlopen", return_value=io.BytesIO(archive_bytes(members))):
                    results = update_assets(dats_dir=root / "dats", cuesheets_dir=root, systems=["psx"], include_dats=False)
                self.assertIsNotNone(results[0].error)
                self.assertEqual(original, existing.read_bytes())
                self.assertEqual({existing}, set(root.iterdir()))
