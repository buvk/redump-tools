import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from redump_engine.actions import VerificationMode
from redump_engine.cli import build_parser
from redump_engine.dat_models import DatEntry, DatIndex, DatRom
from redump_engine.dolphin_ops import DolphinTool, parse_verification
from redump_engine.ui_badges import verification_badge
from redump_engine.ui_runner import action_for_command, build_command
from redump_engine.workflow_nintendo_verify import verify_nintendo_image

REPORT = "CRC32: 2c37cf08\nMD5 not computed\nSHA1: 09b059208054434df2dadf20b96292b8e11fa908\nProblems Found: No\n"


class NintendoVerificationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("redump_engine.workflow_nintendo_verify.item_for", return_value=SimpleNamespace(system=None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parses_installed_dolphin_report_without_md5(self):
        result = parse_verification(REPORT)
        self.assertFalse(result.problems)
        self.assertEqual("2c37cf08", result.crc32)
        with self.assertRaises(ValueError):
            parse_verification("Problems Found: No")

    def fixture(self, problems=False, system="gamecube", crc="2c37cf08"):
        report = parse_verification(REPORT.replace("Found: No", "Found: Yes") if problems else REPORT)
        tool = Mock(spec=DolphinTool)
        tool.verify.return_value = report
        entry = DatEntry("1080 Avalanche (USA)", [DatRom("1080 Avalanche (USA).iso", crc=crc, sha1=report.sha1)], system)
        return tool, DatIndex([entry], {}, {report.sha1: [entry]})

    def test_image_matches_dat_without_a_manifest(self):
        tool, index = self.fixture()
        result = verify_nintendo_image(Path("Missing/Game.rvz"), tool, index)
        self.assertEqual("Passed", result.status)
        self.assertEqual("1080 Avalanche (USA)", result.name)
        self.assertIn("directly", result.details)

    def test_problem_report_is_failure_even_with_matching_hash(self):
        tool, index = self.fixture(problems=True)
        self.assertEqual("Failed", verify_nintendo_image(Path("Game.rvz"), tool, index).status)
        self.assertEqual("Failed", verify_nintendo_image(Path("Game.rvz"), tool, None, integrity_only=True).status)

    def test_wrong_crc_is_not_a_dat_match(self):
        tool, index = self.fixture(crc="ffffffff")
        self.assertEqual("Failed", verify_nintendo_image(Path("Game.rvz"), tool, index).status)

    def test_contents_check_requires_system_dat(self):
        for index in (None, self.fixture(system="ps2")[1]):
            tool, _ = self.fixture()
            with self.assertRaisesRegex(ValueError, "DAT unavailable"):
                verify_nintendo_image(Path("Game.rvz"), tool, index)
            tool.verify.assert_not_called()

    def test_integrity_check_needs_no_dat_and_has_distinct_status(self):
        tool, _ = self.fixture()
        result = verify_nintendo_image(Path("Game.rvz"), tool, None, integrity_only=True)
        self.assertEqual("Passed", result.status)
        self.assertIn("does not compare", result.details)
        self.assertEqual(("✓ Image checked", "Success"), verification_badge("Image integrity: Passed"))
        self.assertEqual(("✓ Image DAT match", "Success"), verification_badge("Disc image: Passed"))

    def test_command_round_trip_preserves_verification_kind(self):
        for action, mode in (("nintendo-verify", VerificationMode.IMAGE), ("nintendo-integrity", VerificationMode.IMAGE_INTEGRITY)):
            command = build_command(action, Path("Games"), chdman=Path("chdman"), dats=Path("dats"), cues=Path("cues"))
            args = build_parser().parse_args(command[4:])
            self.assertEqual(action, args.command)
            resolved = action_for_command(command)
            assert resolved is not None
            self.assertEqual(mode, resolved.verification)
