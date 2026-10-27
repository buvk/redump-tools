import unittest
from pathlib import Path

from redump_engine.action_review import conversion_review
from redump_engine.actions import ACTIONS


class ActionReviewTests(unittest.TestCase):
    def test_conversion_summary_covers_outputs_and_retention(self):
        for action, output in (("convert", "CHD"), ("extract", "ISO or BIN/CUE"), ("xiso", "Xbox XISO"),
                               ("rvz", "RVZ"), ("rvz-extract", "ISO")):
            with self.subTest(action=action):
                message = conversion_review(ACTIONS[action], [Path("one.iso"), Path("two.iso")], True, False)
                self.assertIn("Selected: 2 disc images", message)
                self.assertIn(f"Output: {output}", message)
                self.assertIn("Sources: kept", message)
                self.assertIn("Existing outputs: kept", message)

    def test_irreversible_changes_are_in_the_single_review(self):
        message = conversion_review(ACTIONS["convert"], [Path("one.iso")], False, True)
        self.assertIn("deleted only after successful processing", message)
        self.assertIn("may be overwritten", message)
        message = conversion_review(ACTIONS["xiso"], [Path("one.iso")], False, True)
        self.assertIn("cannot be reconstructed", message)
        self.assertIn("Existing outputs: may be overwritten", message)


class CompactPathTests(unittest.TestCase):
    def test_names_are_short_unless_disambiguation_or_full_paths_requested(self):
        from redump_engine.action_review import display_paths
        paths = [Path("Xbox/One/Game.iso"), Path("PS2/Two/Game.iso"), Path("PS2/Unique.iso")]
        self.assertEqual([str(Path("One/Game.iso")), str(Path("Two/Game.iso")), "Unique.iso"], display_paths(paths))
        self.assertEqual(list(map(str, paths)), display_paths(paths, True))
