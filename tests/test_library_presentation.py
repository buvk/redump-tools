from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from redump_engine.library_discovery import item_for
from redump_engine.library_models import LibraryItem
from redump_engine.library_presentation import disc_details, selection_summary, set_summary
from redump_engine.metadata import Manifest, ManifestDisc, save_manifest


class SelectionSummaryTests(unittest.TestCase):
    def test_set_uses_weighted_savings_and_aggregate_verification(self):
        items = [LibraryItem(Path("One.rvz"), "RVZ", 100, Path("manifest.json"), original_size=1000, missing_record=False),
                 LibraryItem(Path("Two.rvz"), "RVZ", 900, Path("manifest.json"), original_size=9000, missing_record=False)]
        result = set_summary(items, ["Manifest: Passed", "Manifest: Passed"], [1, 2], 2)
        self.assertEqual(result[0], "1,000 B")
        self.assertIn("90.0%", result[2])
        self.assertEqual(result[3:], ("✓ Recorded", "✓ All DAT matched"))
        self.assertEqual(set_summary(items, ["Manifest: Passed", "Not checked"], [1, 2], 2)[4], "1/2 DAT matched")
        self.assertEqual(set_summary(items, ["Manifest: Passed", "Manifest: Failed"], [1, 2], 2)[4], "! Failed")
        self.assertEqual(set_summary(items, ["Manifest: Warning", "Not checked"], [1, 2], 2)[4], "! Needs attention")

    def test_filtered_or_unknown_set_does_not_claim_full_verification_or_savings(self):
        one = LibraryItem(Path("One.chd"), "CHD", 100, None, original_size=1000)
        unknown = LibraryItem(Path("Two.chd"), "CHD", 100, None)
        result = set_summary([one, unknown], ["Manifest: Passed", "Not checked"], [1, 2], 2)
        self.assertEqual(result[1:3], ("Partial", "Partial"))
        self.assertEqual(set_summary([one], ["Manifest: Passed"], [1], 2)[4], "1/2 DAT matched")

    def test_set_counts_original_disc_once_with_multiple_formats(self):
        items = [LibraryItem(Path("One.iso"), "ISO", 1000, None, original_size=1000),
                 LibraryItem(Path("One.chd"), "CHD", 400, None, original_size=1000)]
        result = set_summary(items, ["Manifest: Passed", "Manifest: Passed"], [1, 1], 2)
        self.assertEqual(result[1], "1,000 B")
        self.assertIn("40.0% larger", result[2])
        self.assertEqual(result[4], "1/2 DAT matched")

    def mixed_format_items(self, root, system="Sony - PlayStation", title="Game"):
        folder = root / system / title
        folder.mkdir(parents=True)
        paths = [folder / f"{title} (Disc 1).iso", folder / f"{title} (Disc 1).chd", folder / f"{title} (Disc 2).chd"]
        for path, size in zip(paths, (1000, 400, 400), strict=True):
            path.write_bytes(b"x" * size)
        save_manifest(folder / "manifest.json", Manifest(1, "iso", title, [
            ManifestDisc(1, [{"name": paths[0].name, "kind": "iso", "size": 1000}], [paths[0].name, paths[1].name]),
            ManifestDisc(2, [{"name": f"{title} (Disc 2).iso", "kind": "iso", "size": 1000}], [paths[2].name])]))
        return [item_for(path) for path in paths]

    def test_selection_and_set_totals_agree_for_mixed_formats_without_more_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            items = self.mixed_format_items(Path(temporary))
            self.assertEqual(items[0].original_id, items[1].original_id)
            self.assertNotEqual(items[0].original_id, items[2].original_id)
            with patch("redump_engine.library_presentation.load_manifest", side_effect=AssertionError("UI reread metadata")):
                text = selection_summary([*items, items[0]])
            grouped = set_summary(items, ["Not checked"] * 3, [1, 1, 2], 2)
            self.assertIn(f"Current: {grouped[0]}", text)
            self.assertIn(f"Original: {grouped[1]}", text)
            self.assertIn("Saved: 200 B (10.0%)", text)
            self.assertIn("200 B (10.0%)", grouped[2])
            # One representation can supply the known size for the same disc.
            partial_alias = replace(items[1], original_size=None)
            self.assertIn("Original: 1.95 KiB", selection_summary([items[0], partial_alias, items[2]]))
            unknown = LibraryItem(Path(temporary) / "Unknown.chd", "CHD", 50, None)
            self.assertIn("Original: 1.95 KiB + 1 unknown", selection_summary([*items, unknown]))
            self.assertNotIn("Saved:", selection_summary([*items, unknown]))

    def test_original_identity_keeps_different_games_and_systems_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one = self.mixed_format_items(root)
            other_game = self.mixed_format_items(root, title="Other game")
            other_system = self.mixed_format_items(root, system="Sega - Dreamcast")
            self.assertNotEqual(one[0].original_id, other_game[0].original_id)
            self.assertNotEqual(one[0].original_id, other_system[0].original_id)
            self.assertIn("Original: 5.86 KiB", selection_summary([*one, *other_game, *other_system]))
            self.assertIn("(10.0%)", selection_summary([*one, *other_game, *other_system]))

    def test_set_success_preserves_dat_integrity_and_mixed_scopes(self):
        items = [LibraryItem(Path(f"Disc {number}.chd"), "CHD", 100, None) for number in (1, 2)]
        scenarios = (
            (["Manifest: Passed", "Disc image: Passed"], "✓ All DAT matched"),
            (["Original files: Passed", "Original manifest: Passed"], "✓ All DAT matched"),
            (["CHD: Passed", "Image integrity: Passed"], "✓ All integrity checked"),
            (["Manifest: Passed", "Image integrity: Passed"], "✓ All checks passed (mixed)"),
            (["Image integrity: Passed", "Not checked"], "1/2 integrity checked"),
            (["Disc image: Passed", "Not checked"], "1/2 DAT matched"),
            (["CHD: Passed", "Manifest: Failed"], "! Failed"),
            (["Image integrity: Passed", "Manifest: Warning"], "! Needs attention"),
        )
        for labels, expected in scenarios:
            with self.subTest(labels=labels):
                self.assertEqual(expected, set_summary(items, labels, [1, 2], 2)[4])
        self.assertEqual("1/3 integrity checked", set_summary(items, ["CHD: Passed"] * 2, [1, 1], 3)[4])
        self.assertEqual("○ Not checked", set_summary(items, ["CHD: Passed", "Not checked"], [1, 1], 2)[4])

    def test_multiple_discs_have_one_compact_size_summary(self) -> None:
        items = [LibraryItem(Path("One.chd"), "CHD", 400, None, original_size=1000),
                 LibraryItem(Path("Two.chd"), "CHD", 300, None, original_size=900)]
        text = selection_summary(items)
        self.assertIn("Current: 700 B", text)
        self.assertIn("Original: 1.86 KiB", text)
        self.assertIn("Saved: 1.17 KiB", text)
        self.assertNotIn("One.chd", text)

    def test_partial_original_sizes_are_marked_unknown_without_claiming_savings(self) -> None:
        items = [LibraryItem(Path("One.chd"), "CHD", 400, None, original_size=1000),
                 LibraryItem(Path("Two.chd"), "CHD", 300, None)]
        text = selection_summary(items)
        self.assertIn("Original: 1,000 B + 1 unknown", text)
        self.assertNotIn("Saved:", text)

    def test_shared_cue_tracks_and_repeated_rows_are_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            track = folder / "Shared.bin"
            track.write_bytes(b"x" * 100)
            first = LibraryItem(folder / "One.cue", "CUE/BIN", 110, None, (track,))
            second = LibraryItem(folder / "Two.cue", "CUE/BIN", 110, None, (track,))
            self.assertIn("Current: 120 B", selection_summary([first, first, second]))

    def test_details_keep_full_paths_and_format_caveats(self) -> None:
        item = LibraryItem(Path("Library/Xbox/Game.xiso.iso"), "XISO", 100, None)
        text = disc_details(item, "Not checked")
        self.assertIn(str(item.path), text)
        self.assertIn("cannot be reconstructed", text)

    def test_empty_selection_and_expanded_output_are_not_misreported(self) -> None:
        self.assertEqual("", selection_summary([]))
        item = LibraryItem(Path("Game.chd"), "CHD", 200, None, original_size=100)
        self.assertIn("Larger by: 100 B (100.0%)", selection_summary([item]))
