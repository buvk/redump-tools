from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .actions import ACTIONS
from .chd_ops import Chdman, ChdmanError
from .conversion_naming import conversion_plan
from .cue import bin_files_from_cue, count_tracks_in_cue, cue_reference_root, referenced_bin_files
from .file_io import write_metadata
from .file_transaction import FileTransaction
from .filenames import dat_folder_name, is_xiso, parse_disc_number
from .game_folder_merge import merge_game_folder
from .game_manifests import merge_game_manifest
from .library import item_for, manifest_for
from .metadata import (
    DatEntry,
    DatIndex,
    Manifest,
    ManifestDisc,
    PayloadFile,
    describe_dat_match_ambiguity,
    has_unresolved_name_ambiguity,
    load_manifest,
    match_dat_entries,
    match_dat_entries_for_files,
    payload_files_from_paths,
    save_manifest,
    select_dat_entry,
)
from .playlists import playlist_updates
from .source_guard import SourceGuard


@dataclass
class SourceDisc:
    disc_number: int
    source_file: Path
    file_type: str
    tracks: int
    dat_entry: DatEntry | None
    verified_dat_name: bool = False


class ConvertReport(TypedDict):
    game_dir: str
    status: str
    reason: str
    discs: int
    dat_name: str
    created: list[str]


class ConvertWorkflow:
    def __init__(self, chdman: Chdman, dat_index: DatIndex | None, dry_run: bool = False, verbose: bool = False,
                 keep_sources: bool = False, rename_folder: bool = True, use_redump_names: bool = False,
                 on_warning: Callable[[str], None] | None = None):
        self.chdman = chdman
        self.dat_index = dat_index
        self.dry_run = dry_run
        self.verbose = verbose
        self.keep_sources = keep_sources
        self.rename_folder = rename_folder
        self.use_redump_names = use_redump_names
        self.on_warning = on_warning

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[verbose][convert] {message}")

    def process_game_dir(self, game_dir: Path, selected_files: list[Path] | None = None) -> ConvertReport:
        self._log(f"processing game directory: {game_dir}")
        inputs = selected_files if selected_files is not None else [path for path in game_dir.iterdir() if path.suffix.lower() in {".iso", ".cue"}]
        guarded = list(dict.fromkeys([*inputs, *(track for path in inputs if path.suffix.lower() == ".cue" for track in bin_files_from_cue(path))]))
        guard = SourceGuard(guarded)
        sources = self._discover_sources(game_dir, selected_files)
        guard.check()
        for source in sources:
            system = item_for(source.source_file).system or (source.dat_entry.system if source.dat_entry else None)
            if not ACTIONS["convert"].supports("ISO" if source.file_type == "iso" else "CUE/BIN", system):
                raise ChdmanError(f"CHD conversion is unavailable for system {system}; select its console-specific action")
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
        if selected_files is not None:
            if len(sources) != 1:
                raise ChdmanError("Selected conversion requires exactly one ISO or CUE set")
            dat_name = sources[0].dat_entry.name if sources[0].dat_entry else sources[0].source_file.stem
        if sources[0].dat_entry is None:
            existing_path = manifest_for(sources[0].source_file)
            existing = load_manifest(existing_path) if existing_path else None
            if existing:
                dat_name = existing.title
        self._log(f"resolved dat_name={dat_name}, discs={len(sources)}")
        total_discs = len(sources)
        if self.use_redump_names and not self.rename_folder:
            raise ChdmanError("Redump naming must rename the CHD and game folder together")
        plan = conversion_plan(game_dir, sources, dat_name, use_redump_names=self.use_redump_names, selected=selected_files is not None)
        if plan.folder != game_dir and self._shared_tracks(sources):
            raise ChdmanError("Cannot rename or combine this game folder while other CUE files reference its tracks; keep the folder name or resolve the shared references first")
        output_by_source = plan.outputs
        if not self.dry_run and not self.chdman.force:
            existing_outputs = sorted(path.name for path in output_by_source.values() if path.exists())
            if existing_outputs:
                raise ChdmanError(
                    "Refusing to start conversion because output already exists (use --force): "
                    + ", ".join(existing_outputs)
                )

        created_chds: list[Path] = []
        manifest_discs: list[ManifestDisc] = []
        playlist = game_dir / f"{plan.folder.name}.m3u"
        playlist_changes = playlist_updates(game_dir, plan.folder, output_by_source)
        metadata_paths = {game_dir / "manifest.json", plan.manifest, playlist, *game_dir.glob("*.manifest.json"), *game_dir.glob("*.m3u"), *playlist_changes}
        original_dir = game_dir

        with FileTransaction() as transaction:
            if not self.dry_run:
                transaction.watch_metadata(metadata_paths)
            for source in sources:
                output_chd = output_by_source[source.source_file]
                if not self.dry_run:
                    transaction.prepare_output(output_chd)
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
                self._verify_created_chd(output_chd)

                guard.check()
                payload_files = self._payload_files_for_source(source)
                guard.check()
                manifest_discs.append(
                    ManifestDisc(
                        disc=source.disc_number,
                        payload_files=payload_files,
                        source_files=[source.source_file.name, output_chd.name],
                    )
                )
            manifest = Manifest(1, sources[0].file_type, dat_folder_name(dat_name), manifest_discs,
                                sources[0].dat_entry.system if sources[0].dat_entry else None)
            if not self.dry_run:
                if plan.retitled_manifest:
                    save_manifest(plan.manifest, plan.retitled_manifest)
                guard.check()
                merge_game_manifest(plan.manifest, manifest)
                for path, content in playlist_changes.items():
                    write_metadata(path, content)
                if total_discs > 1:
                    self._write_m3u(game_dir, playlist.stem, created_chds)
                if plan.folder != game_dir:
                    try:
                        if plan.merge_folder:
                            merge_game_folder(game_dir, plan.folder, dat_name, transaction, source_guard=guard)
                        else:
                            transaction.rename_folder(game_dir, plan.folder)
                    except OSError as exc:
                        raise ChdmanError(f"Conversion rolled back: could not update the game folder at {plan.folder}. Close programs using the folder and retry. {exc}") from exc
                    guard.remap(game_dir, plan.folder)
                    game_dir = plan.folder
                    created_chds = [game_dir / path.relative_to(original_dir) for path in created_chds]
                    for source in sources:
                        source.source_file = game_dir / source.source_file.relative_to(original_dir)
                guard.check(content=True)
            elif plan.folder != game_dir:
                action = "combine game folders" if plan.merge_folder else "rename folder"
                print(f"[convert] Would {action}: {game_dir} -> {plan.folder}", flush=True)
                created_chds = [plan.folder / path.relative_to(game_dir) for path in created_chds]

        guard.check()
        if not self.keep_sources:
            try:
                self._cleanup_sources(sources)
            except OSError as exc:
                detail = f"CHD conversion completed in {game_dir}, but some source files could not be removed: {exc}"
                print(f"[convert] WARNING: {detail}", flush=True)
                if self.on_warning:
                    self.on_warning(detail)

        return {
            "game_dir": str(plan.folder if self.dry_run else game_dir),
            "status": "converted",
            "reason": "",
            "discs": total_discs,
            "dat_name": dat_name,
            "created": [str(path) for path in created_chds],
        }

    def _verify_created_chd(self, chd_path: Path) -> None:
        """Ensure the CHD is usable before any source file is deleted."""
        if self.dry_run:
            return
        if not chd_path.is_file() or chd_path.stat().st_size == 0:
            raise ChdmanError(f"CHD was not created or is empty: {chd_path.name}")
        self._log(f"verifying created chd: {chd_path.name}")
        self.chdman.verify(chd_path)

    def _discover_sources(self, game_dir: Path, selected_files: list[Path] | None = None) -> list[SourceDisc]:
        sources: list[SourceDisc] = []

        cue_files = sorted(path for path in selected_files if path.suffix.lower() == ".cue") if selected_files is not None else sorted(game_dir.glob("*.cue"))
        iso_files = sorted(path for path in selected_files if path.suffix.lower() == ".iso") if selected_files is not None else sorted(game_dir.glob("*.iso"))
        iso_files = [path for path in iso_files if not is_xiso(path)]

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
        disc_numbers = [source.disc_number for source in sources]
        duplicates = sorted({number for number in disc_numbers if disc_numbers.count(number) > 1})
        if duplicates:
            raise ChdmanError(
                "Duplicate disc numbers in source set: " + ", ".join(str(number) for number in duplicates)
            )
        if len(disc_numbers) > 1:
            expected = set(range(min(disc_numbers), max(disc_numbers) + 1))
            missing = sorted(expected.difference(disc_numbers))
            if missing:
                raise ChdmanError(
                    "Disc-number gap in source set; missing: " + ", ".join(str(number) for number in missing)
                )
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
        if not self.dry_run or self.use_redump_names:
            checksum_dat_matches = match_dat_entries_for_files(checksum_paths, self.dat_index)

        dat_entry = self._resolve_source_dat_entry(
            name_dat_matches,
            checksum_dat_matches,
            source_type,
            source_file.name,
        )
        disc = parse_disc_number(source_file.stem) or (parse_disc_number(dat_entry.name) if dat_entry else None) or 1
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
            verified_dat_name=len(checksum_dat_matches) == 1 and dat_entry is checksum_dat_matches[0],
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

    _count_tracks_in_cue = staticmethod(count_tracks_in_cue)

    @staticmethod
    def _write_m3u(game_dir: Path, dat_name: str, chd_files: list[Path]) -> None:
        m3u_path = game_dir / f"{dat_name}.m3u"
        lines = [file.name for file in chd_files]
        write_metadata(m3u_path, ("\n".join(lines) + "\n").encode("utf-8"))

    @staticmethod
    def _resolve_dat_name(sources: list[SourceDisc], game_dir: Path) -> str:
        for source in sources:
            if source.dat_entry:
                return dat_folder_name(source.dat_entry.name)
        return game_dir.name

    def _payload_files_for_source(self, source: SourceDisc) -> list[PayloadFile]:
        if source.file_type == "iso":
            return payload_files_from_paths([source.source_file], "iso", base=source.source_file.parent)

        bins = self._bin_files_from_cue(source.source_file)
        return payload_files_from_paths(bins, "bin", base=source.source_file.parent)

    def _shared_tracks(self, sources: list[SourceDisc]) -> set[Path]:
        cues = [source.source_file for source in sources if source.file_type == "bin-cue"]
        if not cues:
            return set()
        tracks = {track.resolve() for cue in cues for track in self._bin_files_from_cue(cue)}
        return tracks & referenced_bin_files(cue_reference_root(cues[0].parent), cues)

    def _cleanup_sources(self, sources: list[SourceDisc]) -> None:
        if self.dry_run:
            return

        shared_tracks = self._shared_tracks(sources)
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
                    if path.resolve() in shared_tracks:
                        self._log(f"keeping shared source bin: {path.name}")
                        continue
                    self._log(f"cleanup remove source bin: {path.name}")
                    path.unlink(missing_ok=True)

    _bin_files_from_cue = staticmethod(bin_files_from_cue)

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
