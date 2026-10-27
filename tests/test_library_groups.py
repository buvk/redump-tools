from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from redump_engine.events import ResultRecord
from redump_engine.library_groups import group_games
from redump_engine.library_models import LibraryItem
from redump_engine.ui_layout import library_layout, window_layout
from redump_engine.ui_results import result_summary


class LibraryPresentationTests(unittest.TestCase):
    def test_separate_disc_folders_and_multiple_formats_form_one_game(self) -> None:
        items = [LibraryItem(Path("PS2") / f"Game (Disc {disc})" / f"Game (Disc {disc}).{suffix}",
                             "CHD" if suffix == "chd" else "ISO", 100, None, system="ps2")
                 for disc, suffix in ((1, "iso"), (1, "chd"), (2, "iso"))]
        groups = group_games(items)
        self.assertEqual(1, len(groups))
        self.assertEqual("Game", groups[0].title)
        self.assertEqual((0, 1, 2), groups[0].indices)
        self.assertEqual({0: 1, 1: 1, 2: 2}, groups[0].numbers)
        self.assertEqual(2, groups[0].total_discs)
        self.assertEqual(groups[0].id, group_games(list(reversed(items)))[0].id)

    def test_grouping_keeps_systems_regions_and_unknown_folders_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            items = [LibraryItem(Path(tmp) / folder / name, "CHD", 1, None, system=system)
                     for folder, name, system in (("PS2", "Game (USA) (Disc 1).chd", "ps2"),
                                                  ("PS1", "Game (USA) (Disc 2).chd", "psx"),
                                                  ("PS2", "Game (Europe) (Disc 2).chd", "ps2"),
                                                  ("Unknown A", "Other (Disc 1).chd", None),
                                                  ("Unknown B", "Other (Disc 2).chd", None))]
            self.assertTrue(all(len(group.indices) == 1 for group in group_games(items)))

    def test_single_disc_format_variants_are_not_labelled_as_multidisc(self) -> None:
        items = [LibraryItem(Path(f"Game.{suffix}"), kind, 10, None) for suffix, kind in (("iso", "ISO"), ("chd", "CHD"))]
        self.assertEqual([], group_games(items))

    def test_invalid_saved_layout_values_use_safe_defaults(self) -> None:
        state = library_layout({"filter": [], "sort_column": {}, "columns": {"name": -1, "size": True, "kind": 160},
                                "sidebar_width": 999999, "groups": {"other": True, "game:ok": False}})
        self.assertEqual("All discs", state["filter"])
        self.assertEqual("name", state["sort_column"])
        self.assertEqual({"kind": 160}, state["columns"])
        self.assertEqual({"game:ok": False}, state["groups"])
        self.assertEqual(230, state["sidebar_width"])
        self.assertEqual(("1280x800", True), window_layout({"width": 3000, "height": 2000, "maximized": True}, 1280, 800))

    def test_result_summary_preserves_full_diagnostics_in_record(self) -> None:
        full = "Output: E:\\Library\\" + "Long game name " * 30 + ".chd"
        result = ResultRecord("Game.cue", "Completed", full)
        self.assertLess(len(result_summary(result)), 100)
        self.assertEqual(full, result.details)
        failure = ResultRecord("Game", "Failed", "Important error\n" + "diagnostics " * 100)
        self.assertTrue(result_summary(failure).startswith("Important error"))
        self.assertLessEqual(len(result_summary(failure)), 100)
        repair = ResultRecord("Game", "Completed", "Game\n\nUpdate metadata/references:\n  E:/Library/Game/manifest.json",
                              summary="Naming/layout updated; see full details.")
        self.assertEqual("Naming/layout updated; see full details.", result_summary(repair))
