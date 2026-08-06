from __future__ import annotations

import contextlib
import hashlib
import io
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from redump_engine.chd_ops import Chdman
from redump_engine.cuesheets import CueRepository
from redump_engine.metadata import DatEntry, DatRom, load_dat_index
from redump_engine.updater import selected_assets, update_assets
from redump_engine.workflow_convert import ConvertWorkflow, SourceDisc
from redump_engine.workflow_extract import ExtractWorkflow


def _dat_xml(name: str, rom_name: str, payload: bytes) -> str:
    sha1 = hashlib.sha1(payload).hexdigest()
    crc32 = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
    return (
        f"<datafile><game name='{name}'>"
        f"<rom name='{rom_name}' size='{len(payload)}' sha1='{sha1}' crc='{crc32}'/>"
        f"</game></datafile>"
    )


class DummyChdman(Chdman):
    def __init__(self) -> None:
        super().__init__(Path("dummy-chdman.exe"), dry_run=True)


class ConvertWorkflowHarness(ConvertWorkflow):
    def discover_sources(self, game_dir: Path) -> list[SourceDisc]:
        return self._discover_sources(game_dir)


class ExtractWorkflowHarness(ExtractWorkflow):
    def heuristic_decision(self, logical_size: int | None, track_count: int, dat_entry: DatEntry | None) -> str:
        return self._heuristic_decision(logical_size, track_count, dat_entry)

    def log_heuristic_override(
        self,
        logical_size: int | None,
        track_count: int,
        dat_entry: DatEntry | None,
        decision: str,
    ) -> None:
        self._log_heuristic_override(logical_size, track_count, dat_entry, decision)

    def replace_cue_with_trusted_source(
        self,
        dat_name: str,
        extracted: list[Path],
        decision: str,
        dat_entry: DatEntry | None,
    ) -> None:
        self._replace_cue_with_trusted_source(dat_name, extracted, decision, dat_entry)

    def resolve_dat_entry_for_decision(
        self,
        name_dat_matches: list[DatEntry],
        checksum_dat_matches: list[DatEntry],
        decision: str,
        source_name: str,
    ) -> DatEntry | None:
        return self._resolve_dat_entry_for_decision(name_dat_matches, checksum_dat_matches, decision, source_name)


class WorkflowRefactorTests(unittest.TestCase):
    def test_update_asset_selection_filters_systems_and_kinds(self) -> None:
        assets = selected_assets(["psx"], include_dats=False, include_cuesheets=True)

        self.assertEqual(1, len(assets))
        self.assertEqual("psx", assets[0].system)
        self.assertEqual("cue", assets[0].kind)

    def test_update_downloads_to_stable_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            cues = root / "cuesheets"
            downloaded: list[Path] = []

            def fake_download(url: str, destination: Path) -> None:
                destination.write_bytes(url.encode("utf-8"))
                downloaded.append(destination)

            with patch("redump_engine.updater._download_to_path", side_effect=fake_download):
                results = update_assets(
                    dats_dir=dats,
                    cuesheets_dir=cues,
                    systems=["psx", "ps2"],
                    include_dats=True,
                    include_cuesheets=True,
                    dry_run=False,
                )

            self.assertEqual(4, len(results))
            self.assertEqual(
                {
                    dats / "redump_psx_dat.zip",
                    dats / "redump_ps2_dat.zip",
                    cues / "redump_psx_cuesheets.zip",
                    cues / "redump_ps2_cuesheets.zip",
                },
                set(downloaded),
            )
            for destination in downloaded:
                self.assertTrue(destination.exists())

    def test_extract_heuristic_prefers_iso_for_large_single_track(self) -> None:
        workflow = ExtractWorkflowHarness(chdman=DummyChdman(), dat_index=None, cues=CueRepository(Path(tempfile.gettempdir())))

        dat_entry = DatEntry(name="Tony Hawk's Pro Skater 3 (USA)", roms=[DatRom(name="Tony Hawk's Pro Skater 3 (USA).cue")])
        decision = workflow.heuristic_decision(3873871872, 1, dat_entry)

        self.assertEqual("iso", decision)

    def test_extract_logs_when_large_size_overrides_bin_cue_dat(self) -> None:
        workflow = ExtractWorkflowHarness(chdman=DummyChdman(), dat_index=None, cues=CueRepository(Path(tempfile.gettempdir())), verbose=True)
        dat_entry = DatEntry(name="Tony Hawk's Pro Skater 3 (USA)", roms=[DatRom(name="Tony Hawk's Pro Skater 3 (USA).cue")])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.log_heuristic_override(3873871872, 1, dat_entry, "iso")

        self.assertIn("large logical size overrides DAT format; choosing iso", output.getvalue())

    def test_convert_discovers_iso_source_using_checksum_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            dats.mkdir()

            payload = b"iso-payload"
            wrong_payload = b"wrong-payload"
            (dats / "a.dat").write_text(_dat_xml("Game (USA)", "Game (USA).iso", wrong_payload), encoding="utf-8")
            (dats / "b.dat").write_text(_dat_xml("Game (USA)", "Game (USA).iso", payload), encoding="utf-8")

            game_dir = root / "game"
            game_dir.mkdir()
            iso_path = game_dir / "Game (USA).iso"
            iso_path.write_bytes(payload)

            workflow = ConvertWorkflowHarness(chdman=DummyChdman(), dat_index=load_dat_index(dats))
            sources = workflow.discover_sources(game_dir)

            self.assertEqual(1, len(sources))
            self.assertEqual("iso", sources[0].file_type)
            self.assertEqual(1, sources[0].tracks)
            dat_entry = sources[0].dat_entry
            self.assertIsNotNone(dat_entry)
            assert dat_entry is not None
            self.assertEqual(hashlib.sha1(payload).hexdigest(), dat_entry.roms[0].sha1)

    def test_convert_discovers_cue_source_and_counts_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            dats.mkdir()

            payload = b"bin-payload"
            (dats / "game.dat").write_text(_dat_xml("Game (USA)", "Track 01.bin", payload), encoding="utf-8")

            game_dir = root / "game"
            game_dir.mkdir()
            cue_path = game_dir / "Game (USA).cue"
            bin_path = game_dir / "Track 01.bin"
            bin_path.write_bytes(payload)
            cue_path.write_text(
                'FILE "Track 01.bin" BINARY\n'
                "  TRACK 01 MODE1/2352\n"
                "  TRACK 02 AUDIO\n",
                encoding="utf-8",
            )

            workflow = ConvertWorkflowHarness(chdman=DummyChdman(), dat_index=load_dat_index(dats))
            sources = workflow.discover_sources(game_dir)

            self.assertEqual(1, len(sources))
            self.assertEqual("bin-cue", sources[0].file_type)
            self.assertEqual(2, sources[0].tracks)
            self.assertEqual(cue_path, sources[0].source_file)

    def test_replace_cue_with_trusted_source_retargets_single_bin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cues_root = root / "cues"
            cues_root.mkdir()
            (cues_root / "Game (USA).cue").write_text('FILE "Original.bin" BINARY\n', encoding="utf-8")

            game_dir = root / "game"
            game_dir.mkdir()
            cue_path = game_dir / "Game (USA).cue"
            bin_path = game_dir / "Renamed.bin"
            cue_path.write_text('FILE "Placeholder.bin" BINARY\n', encoding="utf-8")
            bin_path.write_bytes(b"payload")

            workflow = ExtractWorkflowHarness(chdman=DummyChdman(), dat_index=None, cues=CueRepository(cues_root))
            dat_entry = DatEntry(
                name="Game (USA)",
                roms=[DatRom(name="Game (USA).cue"), DatRom(name="Renamed.bin")],
            )

            workflow.replace_cue_with_trusted_source(
                dat_name="Game (USA)",
                extracted=[cue_path, bin_path],
                decision="bin-cue",
                dat_entry=dat_entry,
            )

            self.assertTrue(cue_path.exists())
            self.assertEqual('FILE "Renamed.bin" BINARY\n', cue_path.read_text(encoding="utf-8"))

    def test_replace_cue_with_trusted_source_preserves_multi_bin_cue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cues_root = root / "cues"
            cues_root.mkdir()
            trusted_text = 'FILE "Original.bin" BINARY\n  TRACK 01 MODE1/2352\n'
            (cues_root / "Game (USA).cue").write_text(trusted_text, encoding="utf-8")

            game_dir = root / "game"
            game_dir.mkdir()
            cue_path = game_dir / "Game (USA).cue"
            bin_a = game_dir / "Track 01.bin"
            bin_b = game_dir / "Track 02.bin"
            cue_path.write_text('FILE "Placeholder.bin" BINARY\n', encoding="utf-8")
            bin_a.write_bytes(b"payload-a")
            bin_b.write_bytes(b"payload-b")

            workflow = ExtractWorkflowHarness(chdman=DummyChdman(), dat_index=None, cues=CueRepository(cues_root))
            dat_entry = DatEntry(
                name="Game (USA)",
                roms=[DatRom(name="Game (USA).cue"), DatRom(name="Track 01.bin"), DatRom(name="Track 02.bin")],
            )

            workflow.replace_cue_with_trusted_source(
                dat_name="Game (USA)",
                extracted=[cue_path, bin_a, bin_b],
                decision="bin-cue-split",
                dat_entry=dat_entry,
            )

            self.assertTrue(cue_path.exists())
            self.assertEqual(trusted_text, cue_path.read_text(encoding="utf-8"))

    def test_extract_resolution_prefers_unique_checksum_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = ExtractWorkflowHarness(chdman=DummyChdman(), dat_index=None, cues=CueRepository(root))
            wrong_entry = DatEntry(name="Game (USA)", roms=[DatRom(name="Game (USA).iso", sha1="0" * 40, crc="0" * 8)])
            right_sha1 = hashlib.sha1(b"payload").hexdigest()
            right_crc = f"{zlib.crc32(b'payload') & 0xFFFFFFFF:08x}"
            right_entry = DatEntry(
                name="Game (USA)",
                roms=[DatRom(name="Game (USA).iso", sha1=right_sha1, crc=right_crc)],
            )

            resolved = workflow.resolve_dat_entry_for_decision(
                name_dat_matches=[wrong_entry, right_entry],
                checksum_dat_matches=[right_entry],
                decision="iso",
                source_name="Game (USA).chd",
            )

            self.assertIs(right_entry, resolved)

    def test_extract_logs_unresolved_duplicate_title_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = ExtractWorkflowHarness(chdman=DummyChdman(), dat_index=None, cues=CueRepository(root), verbose=True)
            first_entry = DatEntry(name="Game (USA)", roms=[DatRom(name="Game (USA).iso")])
            second_entry = DatEntry(name="Game (USA)", roms=[DatRom(name="Game (USA).iso")])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                resolved = workflow.resolve_dat_entry_for_decision(
                    name_dat_matches=[first_entry, second_entry],
                    checksum_dat_matches=[],
                    decision="iso",
                    source_name="Game (USA).chd",
                )

            self.assertIs(first_entry, resolved)
            self.assertIn("ambiguous DAT title match remains unresolved", output.getvalue())

    def test_load_dat_index_builds_checksum_lookup_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dats = root / "dats"
            dats.mkdir()
            payload = b"payload"
            sha1 = hashlib.sha1(payload).hexdigest()
            crc32 = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
            (dats / "game.dat").write_text(_dat_xml("Game (USA)", "Game (USA).iso", payload), encoding="utf-8")

            dat_index = load_dat_index(dats)

            self.assertIsNotNone(dat_index)
            assert dat_index is not None
            self.assertIn(sha1, dat_index.by_sha1)
            self.assertIn(crc32, dat_index.by_crc32)
            matched = dat_index.match_by_checksums({sha1}, {crc32})
            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual("Game (USA)", matched.name)


if __name__ == "__main__":
    unittest.main()