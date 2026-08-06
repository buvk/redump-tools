from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .chd_ops import Chdman
from .filenames import dat_folder_name, dat_output_stem, parse_disc_number
from .metadata import (
    DatEntry,
    DatIndex,
    Manifest,
    ManifestDisc,
    PayloadFile,
    describe_dat_match_ambiguity,
    has_unresolved_name_ambiguity,
    match_dat_entries,
    match_dat_entries_for_files,
    payload_files_from_paths,
    save_manifest,
    select_dat_entry,
)


@dataclass
class SourceDisc:
    disc_number: int
    source_file: Path
    file_type: str
    tracks: int
    dat_entry: DatEntry | None


class ConvertReport(TypedDict):
    game_dir: str
    status: str
    reason: str
    discs: int
    dat_name: str
    created: list[str]


class ConvertWorkflow:
    def __init__(self, chdman: Chdman, dat_index: DatIndex | None, dry_run: bool = False, verbose: bool = False):
        self.chdman = chdman
        self.dat_index = dat_index
        self.dry_run = dry_run
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[verbose][convert] {message}")

    def process_game_dir(self, game_dir: Path) -> ConvertReport:
        self._log(f"processing game directory: {game_dir}")
        sources = self._discover_sources(game_dir)
        if not sources:
            self._log("no cue/iso sources found; skipping")
            return {
                "game_dir": str(game_dir),
                "status": "skipped",
                "reason": "no iso/cue source",
                "discs": 0,
                "dat_name": "",
                "created": [],
            }

        dat_name = dat_folder_name(self._resolve_dat_name(sources, game_dir))
        self._log(f"resolved dat_name={dat_name}, discs={len(sources)}")
        total_discs = len(sources)
        created_chds: list[Path] = []
        manifest_discs: list[ManifestDisc] = []

        for source in sources:
            stem = dat_output_stem(dat_name, source.disc_number, total_discs)
            output_chd = game_dir / f"{stem}.chd"
            if source.file_type == "iso":
                self._log(
                    f"createdvd disc={source.disc_number}, input={source.source_file.name}, output={output_chd.name}"
                )
                self.chdman.createdvd(source.source_file, output_chd)
            else:
                self._log(
                    f"createcd disc={source.disc_number}, input={source.source_file.name}, output={output_chd.name}"
                )
                self.chdman.createcd(source.source_file, output_chd)
            created_chds.append(output_chd)

            payload_files = self._payload_files_for_source(source)

            manifest_discs.append(
                ManifestDisc(
                    disc=source.disc_number,
                    payload_files=payload_files,
                )
            )

        if total_discs > 1 and not self.dry_run:
            self._log(f"writing m3u: {dat_name}.m3u")
            self._write_m3u(game_dir, dat_name, created_chds)

        manifest = Manifest(
            schema_version=1,
            set_type=sources[0].file_type,
            title=dat_name,
            disc_entries=manifest_discs,
        )
        if not self.dry_run:
            self._log("writing manifest.json")
            save_manifest(game_dir / "manifest.json", manifest)

        self._cleanup_sources(sources)
        self._rename_folder_if_needed(game_dir, dat_name)

        return {
            "game_dir": str(game_dir),
            "status": "converted",
            "reason": "",
            "discs": total_discs,
            "dat_name": dat_name,
            "created": [str(path) for path in created_chds],
        }

    def _discover_sources(self, game_dir: Path) -> list[SourceDisc]:
        sources: list[SourceDisc] = []

        cue_files = sorted(game_dir.glob("*.cue"))
        iso_files = sorted(game_dir.glob("*.iso"))

        for cue in cue_files:
            sources.append(
                self._discover_source(
                    source_file=cue,
                    source_type="bin-cue",
                    tracks=self._count_tracks_in_cue(cue),
                    checksum_paths=self._bin_files_from_cue(cue),
                )
            )

        for iso in iso_files:
            sources.append(
                self._discover_source(
                    source_file=iso,
                    source_type="iso",
                    tracks=1,
                    checksum_paths=[iso],
                )
            )

        sources.sort(key=lambda s: s.disc_number)
        return sources

    def _discover_source(
        self,
        source_file: Path,
        source_type: str,
        tracks: int,
        checksum_paths: list[Path],
    ) -> SourceDisc:
        disc = parse_disc_number(source_file.stem) or 1
        name_dat_matches = match_dat_entries(source_file.name, self.dat_index)
        checksum_dat_matches: list[DatEntry] = []
        if not self.dry_run:
            checksum_dat_matches = match_dat_entries_for_files(checksum_paths, self.dat_index)

        dat_entry = self._resolve_source_dat_entry(
            name_dat_matches,
            checksum_dat_matches,
            source_type,
            source_file.name,
        )
        self._log(
            f"source {source_type}={source_file.name}, disc={disc}, tracks={tracks}, "
            f"dat_match={dat_entry.name if dat_entry else 'none'} ({self._match_source_label(dat_entry, name_dat_matches, checksum_dat_matches)})"
        )
        return SourceDisc(
            disc_number=disc,
            source_file=source_file,
            file_type=source_type,
            tracks=tracks,
            dat_entry=dat_entry,
        )

    @staticmethod
    def _match_source_label(
        dat_entry: DatEntry | None,
        name_dat_matches: list[DatEntry],
        checksum_dat_matches: list[DatEntry],
    ) -> str:
        name_dat_entry = name_dat_matches[0] if name_dat_matches else None
        checksum_dat_entry = checksum_dat_matches[0] if checksum_dat_matches else None
        if dat_entry is None:
            return "none"
        if dat_entry is checksum_dat_entry and checksum_dat_entry is not None and dat_entry is not name_dat_entry:
            return "checksum"
        if dat_entry is checksum_dat_entry and dat_entry is name_dat_entry and checksum_dat_entry is not None:
            return "name+checksum"
        return "name"

    @staticmethod
    def _count_tracks_in_cue(cue_path: Path) -> int:
        count = 0
        for line in cue_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().lower().startswith("track "):
                count += 1
        return max(count, 1)

    @staticmethod
    def _write_m3u(game_dir: Path, dat_name: str, chd_files: list[Path]) -> None:
        m3u_path = game_dir / f"{dat_name}.m3u"
        lines = [file.name for file in chd_files]
        m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _resolve_dat_name(sources: list[SourceDisc], game_dir: Path) -> str:
        for source in sources:
            if source.dat_entry:
                return dat_folder_name(source.dat_entry.name)
        return game_dir.name

    def _payload_files_for_source(self, source: SourceDisc) -> list[PayloadFile]:
        if source.file_type == "iso":
            return payload_files_from_paths([source.source_file], "iso")

        bins = self._bin_files_from_cue(source.source_file)
        return payload_files_from_paths(bins, "bin")

    def _cleanup_sources(self, sources: list[SourceDisc]) -> None:
        if self.dry_run:
            return

        for source in sources:
            if source.file_type == "iso":
                self._log(f"cleanup remove source iso: {source.source_file.name}")
                source.source_file.unlink(missing_ok=True)
                continue

            if source.file_type == "bin-cue":
                bins = self._bin_files_from_cue(source.source_file)
                self._log(f"cleanup remove source cue: {source.source_file.name}")
                source.source_file.unlink(missing_ok=True)
                for path in bins:
                    self._log(f"cleanup remove source bin: {path.name}")
                    path.unlink(missing_ok=True)

    @staticmethod
    def _bin_files_from_cue(cue_path: Path) -> list[Path]:
        bins: list[Path] = []
        for line in cue_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("file"):
                continue
            parts = stripped.split('"')
            if len(parts) < 2:
                continue
            candidate = cue_path.parent / parts[1]
            if candidate.suffix.lower() == ".bin":
                bins.append(candidate)
        return bins

    def _rename_folder_if_needed(self, game_dir: Path, dat_name: str) -> None:
        desired = dat_folder_name(dat_name)
        if desired == game_dir.name:
            return
        if self.dry_run:
            return
        target = game_dir.parent / desired
        if target.exists():
            return
        self._log(f"renaming folder: {game_dir.name} -> {desired}")
        game_dir.rename(target)

    def _resolve_source_dat_entry(
        self,
        name_dat_matches: list[DatEntry],
        checksum_dat_matches: list[DatEntry],
        source_type: str,
        source_name: str,
    ) -> DatEntry | None:
        if has_unresolved_name_ambiguity(name_dat_matches, checksum_dat_matches):
            self._log(describe_dat_match_ambiguity(source_name, name_dat_matches, checksum_dat_matches))

        return select_dat_entry(
            name_dat_matches,
            checksum_dat_matches,
            desired_format=source_type,
        )
