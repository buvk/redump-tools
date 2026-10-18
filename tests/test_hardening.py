from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import ChdInfo, Chdman, ChdmanError
from redump_engine.cli import run
from redump_engine.cue import bin_files_from_cue, count_tracks_in_cue
from redump_engine.cuesheets import CueRepository
from redump_engine.metadata import ManifestDisc, compute_crc32, compute_hashes, compute_sha1, load_manifest
from redump_engine.updater import update_assets
from redump_engine.workflow_convert import ConvertWorkflow
from redump_engine.workflow_extract import ExtractWorkflow
from redump_engine.workflow_verify import VerifyWorkflow


class FakeChdman(Chdman):
    """Stands in for chdman by copying bytes; a CHD is just a copy of the ISO."""

    def __init__(self, *, verify_ok: bool = True, **kwargs: bool) -> None:
        super().__init__(Path("fake-chdman"), **kwargs)
        self.verify_ok = verify_ok

    def createdvd(self, input_image: Path, output_chd: Path) -> str:
        self._force_flags(output_chd)
        output_chd.write_bytes(input_image.read_bytes())
        return ""

    def verify(self, input_chd: Path) -> str:
        if not self.verify_ok:
            raise ChdmanError("verify failed")
        return ""

    def info(self, input_chd: Path) -> ChdInfo:
        return ChdInfo(
            logical_size=900 * 1024 * 1024,
            track_count=0,
            has_cd_metadata=False,
            has_dvd_metadata=True,
            raw_output="",
        )

    def extractdvd(self, input_chd: Path, output_file: Path) -> str:
        self._force_flags(output_file)
        output_file.write_bytes(input_chd.read_bytes())
        return ""


class CueHelperTests(unittest.TestCase):
    def test_shared_cue_parser_reads_file_and_datafile_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cue = Path(tmp) / "g.cue"
            cue.write_text(
                'FILE "Track 01.bin" BINARY\n  TRACK 01 MODE1/2352\nFILE "Track 02.bin" BINARY\n  TRACK 02 AUDIO\n',
                encoding="utf-8",
            )

            self.assertEqual([Path(tmp) / "Track 01.bin", Path(tmp) / "Track 02.bin"], bin_files_from_cue(cue))
            self.assertEqual(2, count_tracks_in_cue(cue))
            self.assertEqual([], bin_files_from_cue(Path(tmp) / "missing.cue"))

    def test_cue_repository_refuses_ambiguous_fuzzy_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Game 2 (USA).cue").write_text("a", encoding="utf-8")
            (root / "Game 3 (USA).cue").write_text("b", encoding="utf-8")
            repo = CueRepository(root)

            self.assertIsNone(repo.find("Game"))
            self.assertIsNotNone(repo.find("Game 2 (USA)"))


class HashTests(unittest.TestCase):
    def test_single_pass_hashes_match_individual_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.bin"
            path.write_bytes(b"payload" * 1000)

            sha1, crc32 = compute_hashes(path, chunk_size=64)

            self.assertEqual(compute_sha1(path), sha1)
            self.assertEqual(compute_crc32(path), crc32)
            self.assertEqual(hashlib.sha1(b"payload" * 1000).hexdigest(), sha1)


class ChdmanSafetyTests(unittest.TestCase):
    def test_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Game.chd"
            output.write_bytes(b"existing")

            with self.assertRaisesRegex(ChdmanError, "--force"):
                Chdman(Path("chdman.exe")).createdvd(Path("in.iso"), output)

    def test_force_adds_overwrite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Game.chd"
            output.write_bytes(b"existing")
            chdman = Chdman(Path("chdman.exe"), force=True)

            with patch.object(Chdman, "_run", return_value="") as run_mock:
                chdman.createdvd(Path("in.iso"), output)

            self.assertIn("-f", run_mock.call_args.args)


class ConvertSafetyTests(unittest.TestCase):
    def _game_dir(self, root: Path) -> tuple[Path, Path]:
        game_dir = root / "Game (USA)"
        game_dir.mkdir()
        iso = game_dir / "Game (USA).iso"
        iso.write_bytes(b"iso-data")
        return game_dir, iso

    def test_sources_survive_when_chd_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir, iso = self._game_dir(Path(tmp))
            workflow = ConvertWorkflow(chdman=FakeChdman(verify_ok=False), dat_index=None)

            with self.assertRaises(ChdmanError):
                workflow.process_game_dir(game_dir)

            self.assertTrue(iso.exists())
            self.assertFalse((game_dir / "manifest.json").exists())

    def test_convert_then_extract_round_trip_and_verify_rehash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir, iso = self._game_dir(Path(tmp))
            chdman = FakeChdman()

            report = ConvertWorkflow(chdman=chdman, dat_index=None).process_game_dir(game_dir)
            self.assertEqual("converted", report["status"])
            self.assertFalse(iso.exists())
            self.assertTrue((game_dir / "Game (USA).chd").exists())

            results = ExtractWorkflow(
                chdman=chdman, dat_index=None, cues=CueRepository(Path(tmp) / "cues")
            ).process_game_dir(game_dir)
            self.assertEqual("iso", results[0].decision)
            self.assertEqual(b"iso-data", (game_dir / "Game (USA).iso").read_bytes())
            self.assertFalse((game_dir / "Game (USA).chd").exists())

            # No DAT available, so only the on-disk rehash is meaningful: re-extraction preserves the manifest.
            manifest = json.loads((game_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha1(b"iso-data").hexdigest(), manifest["discs"][0]["payload_files"][0]["sha1"])

    def test_verify_rehash_detects_modified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir, iso = self._game_dir(Path(tmp))
            ConvertWorkflow(chdman=FakeChdman(), dat_index=None).process_game_dir(game_dir)
            ExtractWorkflow(
                chdman=FakeChdman(), dat_index=None, cues=CueRepository(Path(tmp) / "cues")
            ).process_game_dir(game_dir)
            (game_dir / "Game (USA).iso").write_bytes(b"tampered")

            result = VerifyWorkflow(dat_index=None, rehash=True)
            problems = result._rehash_problems(game_dir, _first_disc(game_dir))

            self.assertEqual(1, len(problems))
            self.assertIn("checksum mismatch", problems[0])


def _first_disc(game_dir: Path) -> ManifestDisc:
    manifest = load_manifest(game_dir / "manifest.json")
    assert manifest is not None
    return manifest.disc_entries[0]


class UpdaterSafetyTests(unittest.TestCase):
    def test_invalid_download_keeps_existing_archive_and_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            dats.mkdir()
            existing = dats / "redump_psx_dat.zip"
            with zipfile.ZipFile(existing, "w") as archive:
                archive.writestr("a.dat", "good")
            original = existing.read_bytes()

            class FakeResponse(io.BytesIO):
                def __enter__(self) -> FakeResponse:
                    return self

                def __exit__(self, *exc: object) -> None:
                    self.close()

            with patch("redump_engine.updater.urlopen", return_value=FakeResponse(b"<html>error</html>")):
                results = update_assets(
                    dats_dir=dats,
                    cuesheets_dir=root / "cuesheets",
                    systems=["psx"],
                    include_cuesheets=False,
                )

            self.assertEqual(1, len(results))
            self.assertIsNotNone(results[0].error)
            self.assertEqual(original, existing.read_bytes())
            self.assertEqual([existing], list(dats.iterdir()))


class CliTests(unittest.TestCase):
    def test_stray_quote_from_trailing_backslash_is_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run(["verify", "--root", tmp + '"'])

            self.assertEqual(0, code)

    def test_missing_root_gives_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as raised:
                run(["verify", "--root", str(Path(tmp) / "nope")])

            self.assertEqual(2, raised.exception.code)
            self.assertIn("--root is not an existing directory", err.getvalue())

    def test_root_option_is_accepted_after_subcommand_and_scans_platform_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = Path(tmp) / "Sony - PlayStation 2"
            game = platform / "Game (USA)"
            game.mkdir(parents=True)
            (game / "manifest.json").write_text("{}", encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run(["verify", "--root", str(platform)])

            self.assertIn("Game (USA)", output.getvalue())
            self.assertEqual(2, code)

    def test_global_dry_run_flag_survives_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            chd = game_dir / "Game.chd"
            chd.write_bytes(b"x")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run(["--root", tmp, "--dry-run", "--chdman", "missing.exe", "extract", "--game-dir", str(game_dir)])

            self.assertEqual(0, code)
            self.assertTrue(chd.exists())

    def test_extract_reports_when_nothing_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run(["--root", tmp, "extract", "--game-dir", tmp])

            self.assertEqual(0, code)
            self.assertIn("nothing to do", output.getvalue())

    def test_batch_continues_after_a_failing_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = Path(tmp) / "Platform"
            for name in ("A", "B"):
                game = platform / name
                game.mkdir(parents=True)
                (game / f"{name}.chd").write_bytes(b"x")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run(["--root", tmp, "--chdman", "missing.exe", "extract"])

            text = output.getvalue()
            self.assertEqual(1, code)
            self.assertIn("ERROR A", text)
            self.assertIn("ERROR B", text)
            self.assertIn("errors=2", text)


if __name__ == "__main__":
    unittest.main()
