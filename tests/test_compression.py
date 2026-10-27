from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from redump_engine.library import item_for, scan_library
from redump_engine.metadata import Manifest, ManifestDisc, PayloadFile, save_manifest


class CompressionDisplayTests(unittest.TestCase):
    def test_single_disc_chd_reports_original_size_and_savings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chd = folder / "Game.chd"
            chd.write_bytes(b"x" * 400)
            save_manifest(folder / "Game.manifest.json", Manifest(1, "iso", "Game", [ManifestDisc(1, [
                {"name": "Game.iso", "kind": "iso", "size": 1000}])]))
            item = item_for(chd)
            self.assertEqual(1000, item.original_size)
            self.assertEqual(600, item.saved_bytes)
            self.assertEqual(60.0, item.saved_percent)
            self.assertFalse((folder / "Game.iso").exists())

    def test_multidisc_manifest_uses_matching_disc_not_whole_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chd = folder / "Game (Disc 2).chd"
            chd.write_bytes(b"x" * 400)
            save_manifest(folder / "Game.manifest.json", Manifest(1, "iso", "Game", [
                ManifestDisc(1, [{"name": "Game (Disc 1).iso", "kind": "iso", "size": 9000}]),
                ManifestDisc(2, [{"name": "Game (Disc 2).iso", "kind": "iso", "size": 1000}])]))
            item = next(item for item in scan_library(folder) if item.kind == "CHD")
            self.assertEqual(1000, item.original_size)
            self.assertEqual(60.0, item.saved_percent)

    def test_cd_original_size_sums_all_bin_tracks_without_cue_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chd = folder / "Game.chd"
            chd.write_bytes(b"x" * 500)
            save_manifest(folder / "Game.manifest.json", Manifest(1, "bin-cue", "Game", [ManifestDisc(1, [
                {"name": "Game (Track 1).bin", "kind": "bin", "size": 300},
                {"name": "Game (Track 2).bin", "kind": "bin", "size": 700},
                {"name": "Game.cue", "kind": "cue", "size": 90}])]))
            self.assertEqual(1000, item_for(chd).original_size)
            self.assertEqual(50.0, item_for(chd).saved_percent)

    def test_missing_zero_or_partial_sizes_do_not_produce_misleading_percentages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chd = folder / "Game.chd"
            chd.write_bytes(b"x")
            self.assertIsNone(item_for(chd).saved_percent)
            cases: list[list[PayloadFile]] = [[{"kind": "bin", "size": 10}, {"kind": "bin"}],
                                             [{"kind": "iso", "size": 0}], [{"kind": "iso", "size": -1}],
                                             [{"kind": "iso", "size": "1000"}], [{"kind": "iso", "size": True}]]
            for payloads in cases:
                save_manifest(folder / "Game.manifest.json", Manifest(1, "iso", "Game", [ManifestDisc(1, payloads)]))
                self.assertIsNone(item_for(chd).original_size)
                self.assertIsNone(item_for(chd).saved_percent)

    def test_unsuffixed_chd_with_ambiguous_multidisc_metadata_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chd = folder / "Game.chd"
            chd.write_bytes(b"x")
            save_manifest(folder / "Game.manifest.json", Manifest(1, "iso", "Game", [
                ManifestDisc(number, [{"name": f"Game (Disc {number}).iso", "kind": "iso", "size": 1000}]) for number in (1, 2)]))
            self.assertIsNone(item_for(chd).original_size)

    def test_larger_chds_report_negative_savings_and_uncompressed_files_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chd, iso = folder / "Game.chd", folder / "Game.iso"
            chd.write_bytes(b"x" * 1200)
            iso.write_bytes(b"x" * 1000)
            save_manifest(folder / "Game.manifest.json", Manifest(1, "iso", "Game", [ManifestDisc(1, [
                {"name": "Game.iso", "kind": "iso", "size": 1000}])]))
            self.assertEqual(-200, item_for(chd).saved_bytes)
            self.assertEqual(-20.0, item_for(chd).saved_percent)
            self.assertIsNone(item_for(iso).saved_percent)
