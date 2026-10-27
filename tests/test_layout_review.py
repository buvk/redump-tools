from __future__ import annotations

import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from redump_engine.layout_review import prepare_layout_review


class LayoutReviewTests(unittest.TestCase):
    def test_cancel_before_start_skips_loading_and_planning(self) -> None:
        stop = Event()
        stop.set()
        with patch("redump_engine.layout_review.load_dat_index") as load:
            self.assertIsNone(prepare_layout_review([Path("Game/manifest.json")], Path("dats"), stop, Mock()))
        load.assert_not_called()

    def test_one_refused_game_does_not_discard_reviewable_changes(self) -> None:
        good, bad = Path("Good/manifest.json"), Path("Blocked/manifest.json")
        progress = Mock()
        with patch("redump_engine.layout_review.load_dat_index"), patch("redump_engine.layout_review.plan_layout") as plan:
            plan.side_effect = [Mock(description=Mock(return_value="Good: old → new")), ValueError("Ambiguous checksum")]
            review = prepare_layout_review([good, bad], Path("dats"), Event(), progress)
        assert review is not None
        self.assertEqual(1, review.planned)
        self.assertEqual(("Good: old → new",), review.descriptions)
        self.assertEqual(bad, review.failures[0].source)
        self.assertIn("Will not change:", review.display[1])
        self.assertEqual(2, progress.call_count)

    def test_cancel_between_games_discards_partial_review(self) -> None:
        stop = Event()

        def plan(path: Path, index: object) -> Mock:
            stop.set()
            return Mock(description=Mock(return_value="First plan"))

        with patch("redump_engine.layout_review.load_dat_index"), patch("redump_engine.layout_review.plan_layout", side_effect=plan) as planner:
            review = prepare_layout_review([Path("One/manifest.json"), Path("Two/manifest.json")], Path("dats"), stop, Mock())
        self.assertIsNone(review)
        self.assertEqual(1, planner.call_count)

    def test_full_paths_only_changes_path_display_not_action_wording(self):
        from redump_engine.workflow_layout import LayoutPlan
        root = Path("E:/Library/Game")
        metadata = root / "manifest.json"
        old, new = root / "Old.iso", root / "New.iso"
        plan = LayoutPlan(root, root, metadata, {old: new}, {metadata: b"{}"}, [], {}, {}, "Game")
        compact, full = plan.description(), plan.description(full_paths=True)
        self.assertIn("Update manifest:", compact)
        self.assertIn("Update manifest:", full)
        self.assertNotIn("metadata/references", full)
        self.assertEqual(compact.replace("Old.iso", str(old)).replace("New.iso", str(new)).replace("manifest.json", str(metadata)), full)
