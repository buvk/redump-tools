from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redump_engine.dat_cache import CACHE_NAME
from redump_engine.metadata import load_dat_index


class DatCacheTests(unittest.TestCase):
    def write_dat(self, folder: Path, title: str = "Game") -> Path:
        dat = folder / "test.dat"
        dat.write_text(f'<datafile><header><name>Sony - PlayStation</name></header><game name="{title}">'
                       '<rom name="Game.bin" size="123" sha1="abc" crc="1234"/>'
                       '<rom name="Game.cue" size="10" sha1="cuehash" crc="cuecrc"/></game></datafile>', encoding="utf-8")
        return dat

    def test_cache_reuses_parsed_records_with_identical_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.write_dat(folder)
            original = load_dat_index(folder)
            assert original is not None
            with patch("redump_engine.dat_loader.parse_dat_xml", side_effect=AssertionError("XML parsed again")):
                cached = load_dat_index(folder)
            assert cached is not None
            self.assertEqual(original.entries, cached.entries)
            self.assertEqual(original.name_matches("Game"), cached.name_matches("Game"))
            self.assertEqual(original.checksum_matches({"abc"}, {"1234"}), cached.checksum_matches({"abc"}, {"1234"}))
            self.assertEqual([], cached.checksum_matches({"cuehash"}, set()))
            self.assertEqual("psx", cached.entries[0].system)

    def test_add_change_and_remove_source_invalidate_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            dat = self.write_dat(folder)
            load_dat_index(folder)
            before = dat.stat()
            self.write_dat(folder, "Other")
            os.utime(dat, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            changed = load_dat_index(folder)
            assert changed is not None
            self.assertEqual("Other", changed.entries[0].name)
            (folder / "extra.dat").write_bytes(dat.read_bytes())
            added = load_dat_index(folder)
            assert added is not None
            self.assertEqual(2, len(added.entries))
            dat.unlink()
            (folder / "extra.dat").unlink()
            removed = load_dat_index(folder)
            assert removed is not None
            self.assertEqual([], removed.entries)

    def test_broken_cache_falls_back_to_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.write_dat(folder)
            load_dat_index(folder)
            for broken in ("broken json", "null", '{"version":999}', '[]'):
                (folder / CACHE_NAME).write_text(broken, encoding="utf-8")
                index = load_dat_index(folder)
                assert index is not None
                self.assertEqual("Game", index.entries[0].name)

    def test_read_only_cache_does_not_prevent_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.write_dat(folder)
            with patch("redump_engine.dat_cache.tempfile.NamedTemporaryFile", side_effect=PermissionError("read only")):
                index = load_dat_index(folder)
            assert index is not None
            self.assertEqual("Game", index.entries[0].name)
