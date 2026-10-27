from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from redump_engine.actions import ACTIONS, VerificationMode
from redump_engine.cli import build_parser
from redump_engine.library import LibraryItem
from redump_engine.library_rules import unavailable_reason
from redump_engine.ui_runner import action_for_command, build_command


class ActionContractTests(unittest.TestCase):
    def test_manifest_requires_every_tool_in_a_mixed_selection(self) -> None:
        action = ACTIONS["manifest"]
        self.assertEqual((), action.required_tools([Path("Game.iso"), Path("Game.cue")]))
        self.assertEqual(("chdman",), action.required_tools([Path("Game.CHD")]))
        self.assertEqual(("DolphinTool",), action.required_tools([Path("Game.RVZ")]))
        self.assertEqual(("chdman", "DolphinTool"), action.required_tools([
            Path("One.rvz"), Path("Two.chd"), Path("Three.chd"), Path("Four.iso")]))
        self.assertEqual((), action.required_tools([]))

    def test_fixed_tool_requirements_and_duplicate_removal(self) -> None:
        for action, tool in (("convert", "chdman"), ("extract", "chdman"), ("chd-integrity", "chdman"),
                             ("xiso", "extract-xiso"), ("rvz", "DolphinTool"), ("rvz-extract", "DolphinTool")):
            self.assertEqual((tool,), ACTIONS[action].required_tools([Path("Game.iso")]))
        for action in ("verify", "rehash", "organize", "delete"):
            self.assertEqual((), ACTIONS[action].required_tools([Path("Game.chd"), Path("Other.rvz")]))
        action = replace(ACTIONS["manifest"], tool="chdman")
        self.assertEqual(("chdman", "DolphinTool"), action.required_tools([Path("Game.chd"), Path("Other.rvz")]))

    def test_unavailable_explanations_use_the_same_rules_as_eligibility(self) -> None:
        missing = LibraryItem(Path("Game.chd"), "CHD", 0, None)
        self.assertEqual("Manifest missing", unavailable_reason(missing, "verify"))
        self.assertEqual("", unavailable_reason(missing, "chd-integrity"))
        existing = replace(missing, manifest=Path("manifest.json"), missing_record=False)
        self.assertEqual("Manifest already exists", unavailable_reason(existing, "manifest"))
        with patch("redump_engine.library_rules.original_payloads_available", return_value=False):
            self.assertIn("Original ISO/BIN files missing", unavailable_reason(existing, "rehash"))
        xbox = LibraryItem(Path("Game.iso"), "ISO", 0, None, system="xbox")
        self.assertEqual("Unavailable for this system", unavailable_reason(xbox, "convert"))
        broken = replace(xbox, system="ps2", problem="Missing tracks")
        self.assertEqual("Missing tracks", unavailable_reason(broken, "convert"))
        for item in (missing, existing, xbox, broken):
            for action in ACTIONS:
                self.assertEqual(not unavailable_reason(item, action), item.eligible(action))

    def command(self, action: str) -> list[str]:
        return build_command(action, Path("Game"), chdman=Path("chdman.exe"), dats=Path("dats"), cues=Path("cues"))

    def test_verification_modes_do_not_depend_on_labels(self) -> None:
        with patch.dict(ACTIONS, {"rehash": replace(ACTIONS["rehash"], label="Any translated label")}):
            command = self.command("rehash")
            args = build_parser().parse_args(command[4:])
            self.assertEqual("verify", args.command)
            self.assertTrue(args.rehash)
            self.assertFalse(args.chd_integrity)
            action = action_for_command(command)
            assert action is not None
            self.assertEqual(VerificationMode.ORIGINALS, action.verification)
        args = build_parser().parse_args(self.command("chd-integrity")[4:])
        self.assertTrue(args.chd_integrity)
        self.assertFalse(args.rehash)

    def test_delete_command_keeps_explicit_selection_boundary(self) -> None:
        command = self.command("delete")
        self.assertIn("--confirm-delete", command)
        self.assertNotIn("--game-dir", command)

    def test_unknown_action_is_never_treated_as_conversion(self) -> None:
        self.assertFalse(LibraryItem(Path("Game.iso"), "ISO", 0, None).eligible("unknown-action"))
