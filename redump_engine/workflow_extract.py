from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .chd_ops import ChdInfo, Chdman, ChdmanError
from .cue import bin_files_from_cue
from .cuesheets import CueRepository
from .file_io import write_metadata
from .file_transaction import FileTransaction
from .filenames import dat_folder_name, dat_output_stem, normalize_name, parse_disc_number, rename_if_needed
from .game_manifests import disc_for_source, manifest_destination, merge_game_manifest
from .metadata import (
    DatEntry,
    DatIndex,
    Manifest,
    ManifestDisc,
    PayloadFile,
    compute_sha1,
    describe_dat_match_ambiguity,
    has_unresolved_name_ambiguity,
    load_manifest,
    match_dat_entries,
    match_dat_entries_for_files,
    payload_files_from_paths,
    select_dat_entry,
)
from .playlists import playlist_updates
from .source_guard import SourceGuard
from .systems import system_from_path

CD_THRESHOLD_BYTES = 800 * 1024 * 1024


@dataclass
class ExtractResult:
    game_dir: Path
    extracted_files: list[Path]
    removed_files: list[Path]
    strategy: str
    decision: str
    dat_name: str


class ExtractWorkflow:
    def __init__(
        self,
        chdman: Chdman,
        dat_index: DatIndex | None,
        cues: CueRepository,
        dry_run: bool = False,
        verbose: bool = False,
        keep_sources: bool = False,
        rename_folder: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ):
        self.chdman = chdman
        self.dat_index = dat_index
        self.cues = cues
        self.dry_run = dry_run
        self.verbose = verbose
        self.keep_sources = keep_sources
        self.rename_folder = rename_folder
        self.on_warning = on_warning
        self.input_manifest: Path | None = None
        self.output_manifest: Path | None = None
        self.selected_mode = False

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[verbose][extract] {message}")

    def _warn(self, message: str) -> None:
        print(f"[extract] WARNING: {message}")
        if self.on_warning:
            self.on_warning(message)

    def process_game_dir(self, game_dir: Path, selected_file: Path | None = None,
                         input_manifest: Path | None = None) -> list[ExtractResult]:
        self.selected_mode = selected_file is not None
        self.input_manifest = input_manifest if selected_file is not None else game_dir / "manifest.json"
        self.output_manifest = manifest_destination(selected_file) if selected_file is not None else game_dir / "manifest.json"
        self._log(f"processing game directory: {game_dir}")
        chd_files = [selected_file] if selected_file is not None else sorted(game_dir.glob("*.chd"))
        self._log(f"found chd files: {[p.name for p in chd_files]}")
        results: list[ExtractResult] = []
        for chd_file in chd_files:
            results.append(self._extract_one(game_dir, chd_file))

        if not self.dry_run and results and selected_file is None:
            self._finalize_m3u_after_extract(game_dir, results)

        if not self.dry_run and results and self.rename_folder and selected_file is None:
            desired = dat_folder_name(results[0].dat_name)
            target = game_dir.parent / desired
            if desired and desired != game_dir.name and not target.exists():
                resolved_dir = game_dir.resolve()
                cwd = Path.cwd().resolve()
                if resolved_dir == cwd or resolved_dir in cwd.parents:
                    self._warn(
                        f"extraction completed; keeping folder '{game_dir}' "
                        "because it contains the current working directory. "
                        f"Rename it to '{desired}' after leaving this folder."
                    )
                else:
                    self._log(f"renaming folder: {game_dir.name} -> {desired}")
                    try:
                        game_dir.rename(target)
                    except OSError as exc:
                        if getattr(exc, "winerror", None) != 32:
                            raise
                        self._warn(
                            f"extraction completed; keeping folder '{game_dir}' "
                            f"because it is in use. Rename it to '{desired}' after closing other programs using it."
                        )
                    else:
                        for item in results:
                            item.game_dir = target
                            item.extracted_files = [target / path.relative_to(game_dir) for path in item.extracted_files]
                            item.removed_files = [target / path.relative_to(game_dir) for path in item.removed_files]
        return results

    def _extract_one(self, game_dir: Path, chd_file: Path) -> ExtractResult:
        if self.dry_run:
            result, _ = self._prepare_extract(game_dir, chd_file, game_dir)
            result.removed_files = self._cleanup_after_extract(game_dir, chd_file)
            return result

        guard = SourceGuard([chd_file])
        with tempfile.TemporaryDirectory(dir=game_dir, prefix=".redump_restore_") as temporary:
            staging = Path(temporary)
            result, disc_number_hint = self._prepare_extract(game_dir, chd_file, staging)
            guard.check(content=True)
            destinations = []
            for path in result.extracted_files:
                if path.is_symlink() or not path.resolve().is_relative_to(staging.resolve()):
                    raise ChdmanError(f"Restoration output is outside its temporary folder: {path}")
                target = game_dir / path.relative_to(staging)
                if target.is_symlink() or target.exists() and target.samefile(chd_file):
                    raise ChdmanError(f"Restoration cannot replace its input or a symbolic link: {target}")
                if target.exists() and not self.chdman.force:
                    raise ChdmanError(f"Refusing to overwrite existing file (use --force): {target}")
                destinations.append(target)
            primary = next(path for path in destinations if path.suffix.lower() == (".iso" if result.decision == "iso" else ".cue"))
            playlists = playlist_updates(game_dir, game_dir, {chd_file: primary}) if not self.keep_sources else {}
            with FileTransaction() as transaction:
                transaction.watch_metadata([game_dir / "manifest.json", *game_dir.glob("*.manifest.json"), *playlists])
                for path, target in zip(result.extracted_files, destinations, strict=True):
                    transaction.prepare_output(target)
                    transaction.transfer(path, target)
                result.extracted_files = destinations
                self._write_extract_manifest(game_dir, chd_file, destinations, result.decision, result.dat_name,
                                             disc_number_hint=disc_number_hint, transaction=transaction)
                for path, content in playlists.items():
                    write_metadata(path, content)
                guard.check(content=True)
        guard.check(content=True)
        result.removed_files = self._cleanup_after_extract(game_dir, chd_file)
        return result

    def _source_context(self, path: Path, manifest: Manifest | None) -> tuple[str | None, list[DatEntry], DatEntry | None]:
        system = (manifest.system if manifest else None) or system_from_path(path)
        matches = match_dat_entries(path.name, self.dat_index, system=system)
        systems = {entry.system for entry in matches if entry.system}
        if system is None and len(systems) == 1:
            system = next(iter(systems))
        entry = matches[0] if matches and (system is not None or len(systems) <= 1) else None
        return system, matches, entry

    @staticmethod
    def _info_decision(info: ChdInfo, system: str | None, entry: DatEntry | None) -> tuple[str, bool]:
        if info.has_gd_metadata:
            return "bin-cue-split", True
        if info.has_dvd_metadata:
            return "iso", False
        if system == "dc":
            return "bin-cue-split", True
        return ExtractWorkflow._heuristic_decision(info.logical_size, info.track_count, entry), info.has_cd_metadata

    def _prepare_extract(self, game_dir: Path, chd_file: Path, output_dir: Path) -> tuple[ExtractResult, int | None]:
        manifest = load_manifest(self.input_manifest) if self.input_manifest else None
        system, name_dat_matches, dat_entry = self._source_context(chd_file, manifest)
        is_dreamcast = system == "dc"
        manifest_disc = disc_for_source(chd_file, manifest) if manifest else None
        if manifest and manifest_disc is None and parse_disc_number(chd_file.stem) is None:
            raise ChdmanError("Cannot identify this CHD in its multi-disc manifest; record its source filename or add a disc number before restoration")

        if manifest_disc is not None:
            assert manifest is not None
            manifest_tracks = manifest_disc.track_count
            file_type = manifest.set_type
            if manifest_disc.payload_files:
                file_type = "iso" if all(payload.get("kind") == "iso" for payload in manifest_disc.payload_files) else "bin-cue"
            is_cd_style = file_type == "bin-cue" or manifest_tracks > 1
            self._log(
                "input="
                f"{chd_file.name}, manifest=yes, "
                f"name_dat_match={dat_entry.name if dat_entry else 'none'}, "
                f"manifest_type={manifest.set_type}, manifest_tracks={manifest_tracks}, "
                f"is_cd_style={is_cd_style}"
            )
            decision = self._manifest_decision(file_type, manifest_tracks)
            if is_dreamcast:
                decision, is_cd_style = "bin-cue-split", True
            extracted_files = self._extract_by_decision(
                chd_file,
                output_dir,
                decision,
                dat_entry,
                is_cd_style=is_cd_style,
                track_count_hint=max(manifest_tracks, 1),
            )
            strategy = "manifest"
        else:
            info = self.chdman.info(chd_file)
            is_cd_style = info.has_cd_metadata and not info.has_dvd_metadata
            dat_entry = self._resolve_ambiguous_cd_name_match(chd_file.name, dat_entry, is_cd_style, system)
            self._log(
                "input="
                f"{chd_file.name}, manifest=no, "
                f"name_dat_match={dat_entry.name if dat_entry else 'none'}, "
                f"logical_size={info.logical_size}, track_count={info.track_count}, "
                f"is_cd_style={is_cd_style}"
            )
            decision, is_cd_style = self._info_decision(info, system, dat_entry)
            self._log_heuristic_override(info.logical_size, info.track_count, dat_entry, decision)
            if decision == "unknown":
                extracted_files, decision = self._bruteforce_extract(
                    chd_file,
                    output_dir,
                    dat_entry,
                    is_cd_style=is_cd_style,
                )
                strategy = "bruteforce"
            else:
                extracted_files = self._extract_by_decision(
                    chd_file,
                    output_dir,
                    decision,
                    dat_entry,
                    is_cd_style=is_cd_style,
                    track_count_hint=max(info.track_count, dat_entry.track_count if dat_entry else 1, 1),
                )
                strategy = "heuristic"

        self._validate_extracted_files(extracted_files)
        checksum_dat_matches: list[DatEntry] = []
        if not self.dry_run:
            checksum_dat_matches = match_dat_entries_for_files(extracted_files, self.dat_index)
            checksum_dat_entry = checksum_dat_matches[0] if checksum_dat_matches else None
            self._log(
                "checksum dat match="
                f"{checksum_dat_entry.name if checksum_dat_entry else 'none'}"
            )
        else:
            checksum_dat_entry = None

        resolved_dat_entry = self._resolve_dat_entry_for_decision(
            name_dat_matches,
            checksum_dat_matches,
            decision,
            chd_file.name,
        )
        if dat_entry and checksum_dat_entry and dat_entry is not resolved_dat_entry:
            self._log(
                "name/checksum DAT mismatch; selected alternate DAT entry: "
                f"name='{dat_entry.name}'({dat_entry.expected_format}) "
                f"checksum='{checksum_dat_entry.name}'({checksum_dat_entry.expected_format}) "
                f"decision={decision}"
            )
        manifest_dat_name = manifest.dat_name if manifest and manifest.dat_name else chd_file.stem if self.selected_mode else game_dir.name
        dat_name = resolved_dat_entry.name if resolved_dat_entry else manifest_dat_name
        dat_name = dat_folder_name(dat_name)
        self._log(f"resolved dat_name={dat_name}, decision={decision}, strategy={strategy}")
        extracted_files = self._rename_and_replace_cue(game_dir, extracted_files, resolved_dat_entry, decision,
                                                     name_hint=chd_file.stem if self.selected_mode else None)
        self._validate_extracted_files(extracted_files)
        return ExtractResult(
            game_dir=game_dir,
            extracted_files=extracted_files,
            removed_files=[],
            strategy=strategy,
            decision=decision,
            dat_name=dat_name,
        ), manifest_disc.disc if manifest_disc is not None else parse_disc_number(resolved_dat_entry.name) if resolved_dat_entry else None

    def _validate_extracted_files(self, extracted: list[Path]) -> None:
        if self.dry_run:
            return

        missing = [path for path in extracted if path.is_symlink() or not path.is_file() or path.stat().st_size == 0]
        if missing:
            names = ", ".join(path.name for path in missing)
            raise ChdmanError(f"Extraction output is missing expected files or contains empty/invalid files: {names}")

        if not any(path.suffix.lower() in {".bin", ".iso"} for path in extracted):
            raise ChdmanError("Extraction did not produce an ISO or BIN payload")

    def _write_extract_manifest(
        self,
        game_dir: Path,
        source_chd: Path,
        extracted: list[Path],
        decision: str,
        dat_name: str,
        disc_number_hint: int | None = None,
        transaction: FileTransaction | None = None,
    ) -> None:
        if self.dry_run:
            return

        payload_file = self._primary_payload_file(extracted)
        self._log(
            f"writing manifest for {source_chd.name}: decision={decision}, dat_name={dat_name}, "
            f"payload={payload_file.name if payload_file else 'none'}"
        )

        disc_number = disc_number_hint or parse_disc_number(source_chd.stem) or 1
        payload_files = self._payload_files_from_extracted(extracted)
        new_disc = ManifestDisc(
            disc=disc_number,
            payload_files=payload_files,
            source_files=[source_chd.name, *(path.name for path in extracted if path.suffix.lower() in {".cue", ".iso"})],
        )

        manifest_path = manifest_destination(source_chd, dat_name) if self.selected_mode else self.output_manifest or game_dir / "manifest.json"
        if self.selected_mode and self.input_manifest and self.input_manifest.is_file():
            manifest_path = self.input_manifest
        if transaction is not None:
            transaction.watch_metadata([manifest_path])
        existing = load_manifest(manifest_path)
        if existing:
            merged = [d for d in existing.disc_entries if d.disc != disc_number]
            merged.append(new_disc)
            merged.sort(key=lambda d: d.disc)

            manifest = Manifest(
                schema_version=1,
                set_type="iso" if decision == "iso" else "bin-cue",
                title=dat_name or existing.dat_name,
                disc_entries=merged,
                system=existing.system,
            )
        else:
            manifest = Manifest(
                schema_version=1,
                set_type="iso" if decision == "iso" else "bin-cue",
                title=dat_name,
                disc_entries=[new_disc],
            )
        merge_game_manifest(manifest_path, manifest)

    @staticmethod
    def _manifest_decision(file_type: str, tracks: int) -> str:
        if file_type == "iso":
            return "iso"
        if file_type == "bin-cue" and tracks > 1:
            return "bin-cue-split"
        if file_type == "bin-cue":
            return "bin-cue"
        return "unknown"

    @staticmethod
    def _heuristic_decision(logical_size: int | None, track_count: int, dat_entry: DatEntry | None) -> str:
        if track_count > 1:
            return "bin-cue-split"
        if logical_size and logical_size > CD_THRESHOLD_BYTES:
            return "iso"
        if dat_entry and dat_entry.expected_format == "iso":
            return "iso"
        if dat_entry and dat_entry.expected_format == "bin-cue":
            return "bin-cue-split" if dat_entry.track_count > 1 else "bin-cue"
        return "unknown"

    def _log_heuristic_override(
        self,
        logical_size: int | None,
        track_count: int,
        dat_entry: DatEntry | None,
        decision: str,
    ) -> None:
        if not self.verbose or decision != "iso":
            return
        if logical_size is None or logical_size <= CD_THRESHOLD_BYTES:
            return
        if track_count > 1:
            return
        if dat_entry is None or dat_entry.expected_format == "iso":
            return

        self._log(
            "large logical size overrides DAT format; choosing iso "
            f"(logical_size={logical_size}, dat_format={dat_entry.expected_format})"
        )

    def _resolve_ambiguous_cd_name_match(
        self,
        candidate_name: str,
        dat_entry: DatEntry | None,
        is_cd_style: bool,
        system: str | None = None,
    ) -> DatEntry | None:
        if (
            dat_entry is None
            or not is_cd_style
            or self.dat_index is None
            or dat_entry.expected_format != "iso"
        ):
            return dat_entry

        key = normalize_name(Path(candidate_name).stem)
        candidate_disc = parse_disc_number(Path(candidate_name).stem)
        matches = [entry for entry in self.dat_index.entries if normalize_name(entry.name) == key
                   and (system is None or entry.system in {None, system})]
        if len(matches) < 2:
            return dat_entry

        bin_cue_matches = [entry for entry in matches if entry.expected_format == "bin-cue"]
        if candidate_disc is not None:
            disc_specific = [
                entry for entry in bin_cue_matches if parse_disc_number(entry.name) == candidate_disc
            ]
            if disc_specific:
                chosen = disc_specific[0]
                self._log(
                    "ambiguous CD DAT name match; preferring bin-cue variant: "
                    f"{chosen.name}"
                )
                return chosen

        if bin_cue_matches:
            chosen = bin_cue_matches[0]
            self._log(
                "ambiguous CD DAT name match; preferring bin-cue variant: "
                f"{chosen.name}"
            )
            return chosen

        return dat_entry

    def _extract_by_decision(
        self,
        chd_file: Path,
        game_dir: Path,
        decision: str,
        dat_entry: DatEntry | None,
        is_cd_style: bool,
        track_count_hint: int = 1,
    ) -> list[Path]:
        stem = dat_output_stem(dat_entry.name if dat_entry else chd_file.stem, parse_disc_number(chd_file.stem), 2)

        if self.dry_run:
            if decision == "iso":
                return [game_dir / f"{stem}.iso"]
            if decision == "bin-cue-split":
                track_total = max(track_count_hint, 2)
                bins = [game_dir / f"{stem} (Track {idx:02d}).bin" for idx in range(1, track_total + 1)]
                return [game_dir / f"{stem}.cue", *bins]
            return [game_dir / f"{stem}.cue", game_dir / f"{stem}.bin"]

        if decision == "iso":
            output_iso = game_dir / f"{stem}.iso"
            info = self.chdman.info(chd_file)
            if info.has_cd_metadata and not info.has_dvd_metadata:
                self._log(f"method=extractcd -ob (iso), output={output_iso.name}")
                self.chdman.extractcd_to_iso(chd_file, output_iso)
            else:
                self._log(f"method=extractdvd (iso), output={output_iso.name}")
                self.chdman.extractdvd(chd_file, output_iso)
            return [output_iso]

        output_cue = game_dir / f"{stem}.cue"
        if decision == "bin-cue-split":
            try:
                self._log(f"method=splitbin, output={output_cue.name}")
                self.chdman.splitbin(chd_file, output_cue)
            except ChdmanError:
                self._log("splitbin unavailable; falling back to extractcd for bin/cue")
                self.chdman.extractcd(chd_file, output_cue)
        elif decision == "bin-cue":
            self._log(f"method=extractcd (bin-cue), output={output_cue.name}")
            self.chdman.extractcd(chd_file, output_cue)
        else:
            raise RuntimeError(f"Unknown extraction decision: {decision}")

        extracted = [output_cue]
        extracted.extend(self._bin_files_from_cue(output_cue))
        return extracted

    def _bruteforce_extract(
        self,
        chd_file: Path,
        game_dir: Path,
        dat_entry: DatEntry | None,
        is_cd_style: bool,
    ) -> tuple[list[Path], str]:
        if self.dry_run:
            fallback = "bin-cue"
            if dat_entry and dat_entry.expected_format == "iso":
                fallback = "iso"
            return self._extract_by_decision(chd_file, game_dir, fallback, dat_entry, is_cd_style=is_cd_style), fallback

        with tempfile.TemporaryDirectory(prefix="redump_extract_") as tmp:
            temp_dir = Path(tmp)
            cd_probe_looked_like_cue = False

            if is_cd_style:
                cue_path = temp_dir / "trial.cue"
                self.chdman.extractcd(chd_file, cue_path)
                bin_candidates = self._bin_files_from_cue(cue_path)
                if dat_entry and dat_entry.sha1_set:
                    for candidate in bin_candidates:
                        if compute_sha1(candidate).lower() in dat_entry.sha1_set:
                            return self._extract_by_decision(
                                chd_file,
                                game_dir,
                                "bin-cue",
                                dat_entry,
                                is_cd_style=is_cd_style,
                            ), "bin-cue"

            iso_path = temp_dir / "trial.iso"
            try:
                if is_cd_style:
                    self.chdman.extractcd(chd_file, iso_path)
                    if self._looks_like_cue_descriptor(iso_path):
                        cd_probe_looked_like_cue = True
                        bin_file = self._find_first_bin_from_cue(iso_path)
                        if bin_file is None:
                            sibling_bin = iso_path.with_suffix(".bin")
                            bin_file = sibling_bin if sibling_bin.exists() else None
                        if bin_file is not None:
                            iso_path.unlink(missing_ok=True)
                            bin_file.rename(iso_path)
                else:
                    self.chdman.extractdvd(chd_file, iso_path)
            except ChdmanError:
                pass
            else:
                if dat_entry and dat_entry.sha1_set:
                    if compute_sha1(iso_path).lower() in dat_entry.sha1_set:
                        return self._extract_by_decision(
                            chd_file,
                            game_dir,
                            "iso",
                            dat_entry,
                            is_cd_style=is_cd_style,
                        ), "iso"

                # If the CD probe produced a cue-style descriptor, this CHD behaves like
                # bin/cue payload and should not be forced down the iso extraction path.
                if is_cd_style and cd_probe_looked_like_cue:
                    return self._extract_by_decision(
                        chd_file,
                        game_dir,
                        "bin-cue",
                        dat_entry,
                        is_cd_style=is_cd_style,
                    ), "bin-cue"

                # No match, but ISO extraction completed, so use it as fallback.
                return self._extract_by_decision(
                    chd_file,
                    game_dir,
                    "iso",
                    dat_entry,
                    is_cd_style=is_cd_style,
                ), "iso"

        return self._extract_by_decision(
            chd_file,
            game_dir,
            "bin-cue",
            dat_entry,
            is_cd_style=is_cd_style,
        ), "bin-cue"

    def _cleanup_after_extract(self, game_dir: Path, chd_file: Path) -> list[Path]:
        if self.keep_sources:
            return []
        removed: list[Path] = []
        for path in [chd_file]:
            if self.dry_run:
                removed.append(path)
                continue
            if path.exists():
                self._log(f"cleanup remove: {path.name}")
                path.unlink()
                removed.append(path)
        return removed

    def _rename_and_replace_cue(
        self,
        game_dir: Path,
        extracted: list[Path],
        dat_entry: DatEntry | None,
        decision: str,
        name_hint: str | None = None,
    ) -> list[Path]:
        dat_name = dat_entry.name if dat_entry else name_hint or game_dir.name
        extracted = self._apply_dat_names(extracted, dat_entry)
        self._replace_cue_with_trusted_source(dat_name, extracted, decision, dat_entry)
        return extracted if self.dry_run else self._existing_extracted_files(extracted)

    def _apply_dat_names(self, extracted: list[Path], dat_entry: DatEntry | None) -> list[Path]:
        if not dat_entry:
            return extracted

        self._log(f"enforcing DAT names for {dat_entry.name}")
        renamed = self._enforce_dat_names(extracted, dat_entry)
        self._verify_dat_names(renamed, dat_entry)
        return renamed

    def _replace_cue_with_trusted_source(
        self,
        dat_name: str,
        extracted: list[Path],
        decision: str,
        dat_entry: DatEntry | None,
    ) -> None:
        if self.dry_run or decision == "iso":
            return

        cue = self._first_file_with_suffix(extracted, ".cue")
        if cue is None:
            return

        if cue.exists():
            with tempfile.NamedTemporaryFile(
                dir=cue.parent,
                prefix=f".{cue.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                trusted_cue = Path(handle.name)
            try:
                copied = self.cues.copy_trusted_cue(
                    dat_name,
                    trusted_cue,
                    expected_bin_name=self._expected_bin_name(extracted),
                    system=dat_entry.system if dat_entry else None,
                )
                if not copied and dat_entry is not None:
                    raise RuntimeError(f"Trusted cue not found for DAT entry: {dat_name}")
                if copied:
                    trusted_cue.replace(cue)
                    self._log(f"trusted cue copied: {cue.name}")
            finally:
                trusted_cue.unlink(missing_ok=True)

    @staticmethod
    def _existing_extracted_files(extracted: list[Path]) -> list[Path]:
        return [path for path in extracted if path.exists()]

    @staticmethod
    def _first_file_with_suffix(extracted: list[Path], suffix: str) -> Path | None:
        for path in extracted:
            if path.suffix.lower() == suffix:
                return path
        return None

    @staticmethod
    def _expected_bin_name(extracted: list[Path]) -> str | None:
        bin_files = [path for path in extracted if path.suffix.lower() == ".bin"]
        if len(bin_files) == 1:
            return bin_files[0].name
        return None

    @staticmethod
    def _decision_expected_format(decision: str) -> str | None:
        if decision == "iso":
            return "iso"
        if decision in {"bin-cue", "bin-cue-split"}:
            return "bin-cue"
        return None

    def _resolve_dat_entry_for_decision(
        self,
        name_dat_matches: list[DatEntry],
        checksum_dat_matches: list[DatEntry],
        decision: str,
        source_name: str,
    ) -> DatEntry | None:
        if has_unresolved_name_ambiguity(name_dat_matches, checksum_dat_matches):
            self._log(describe_dat_match_ambiguity(source_name, name_dat_matches, checksum_dat_matches))

        return select_dat_entry(
            name_dat_matches,
            checksum_dat_matches,
            desired_format=self._decision_expected_format(decision),
        )

    def _finalize_m3u_after_extract(self, game_dir: Path, results: list[ExtractResult]) -> None:
        # Existing playlists were updated as each disc was published. Preserve
        # custom names, ordering, and references to discs in other image formats.
        if any(game_dir.glob("*.m3u")) or len(results) < 2:
            return
        decisions = {result.decision for result in results}
        if decisions.issubset({"bin-cue", "bin-cue-split"}):
            entries = self._playlist_entries_for_suffix(results, ".cue")
        elif decisions == {"iso"}:
            entries = self._playlist_entries_for_suffix(results, ".iso")
        else:
            return
        if entries:
            playlist = game_dir / f"{dat_folder_name(results[0].dat_name)}.m3u"
            self._log(f"writing m3u: {playlist.name}")
            write_metadata(playlist, ("\n".join(entry.name for entry in entries) + "\n").encode("utf-8"))

    @staticmethod
    def _playlist_entries_for_suffix(results: list[ExtractResult], suffix: str) -> list[Path]:
        selected: list[Path] = []
        for result in results:
            matches = [p for p in result.extracted_files if p.suffix.lower() == suffix and p.exists()]
            if not matches:
                return []
            selected.append(matches[0])

        selected.sort(key=lambda p: (parse_disc_number(p.stem) or 9999, p.name.lower()))
        return selected

    def _enforce_dat_names(self, extracted: list[Path], dat_entry: DatEntry) -> list[Path]:
        renamed: list[Path] = []

        expected_cue = [r.name for r in dat_entry.roms if Path(r.name).suffix.lower() == ".cue"]
        expected_iso = [r.name for r in dat_entry.roms if Path(r.name).suffix.lower() == ".iso"]
        expected_bins = [r.name for r in dat_entry.roms if Path(r.name).suffix.lower() == ".bin"]

        cue_files = [p for p in extracted if p.suffix.lower() == ".cue"]
        iso_files = [p for p in extracted if p.suffix.lower() == ".iso"]
        bin_files = sorted([p for p in extracted if p.suffix.lower() == ".bin"], key=self._track_sort_key)

        for path in cue_files:
            target_name = expected_cue[0] if expected_cue else path.name
            if path.name != target_name:
                self._log(f"rename cue: {path.name} -> {target_name}")
            renamed.append(rename_if_needed(path, target_name, dry_run=self.dry_run))

        for path in iso_files:
            target_name = expected_iso[0] if expected_iso else path.name
            if path.name != target_name:
                self._log(f"rename iso: {path.name} -> {target_name}")
            renamed.append(rename_if_needed(path, target_name, dry_run=self.dry_run))

        for idx, path in enumerate(bin_files):
            if idx < len(expected_bins):
                target_name = expected_bins[idx]
            elif len(expected_bins) == 1:
                target_name = expected_bins[0]
            else:
                target_name = path.name
            if path.name != target_name:
                self._log(f"rename bin: {path.name} -> {target_name}")
            renamed.append(rename_if_needed(path, target_name, dry_run=self.dry_run))

        return renamed

    def _verify_dat_names(self, extracted: list[Path], dat_entry: DatEntry) -> None:
        expected_names = {
            r.name
            for r in dat_entry.roms
            if Path(r.name).suffix.lower() in {".cue", ".bin", ".iso"}
        }
        actual_names = {p.name for p in extracted if p.suffix.lower() in {".cue", ".bin", ".iso"}}

        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        if missing or extra:
            raise RuntimeError(
                "DAT filename verification failed. "
                f"missing={missing if missing else []}, extra={extra if extra else []}"
            )

    @staticmethod
    def _track_sort_key(path: Path) -> tuple[int, str]:
        text = path.stem.lower()
        marker = "track "
        if marker in text:
            tail = text.split(marker, 1)[1]
            digits = ""
            for ch in tail:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                return (int(digits), path.name)
        return (9999, path.name)

    @staticmethod
    def _primary_payload_file(extracted: list[Path]) -> Path | None:
        iso_files = [p for p in extracted if p.suffix.lower() == ".iso"]
        if iso_files:
            return iso_files[0]
        bin_files = sorted([p for p in extracted if p.suffix.lower() == ".bin"], key=ExtractWorkflow._track_sort_key)
        if bin_files:
            return bin_files[0]
        return None

    @staticmethod
    def _payload_files_from_extracted(extracted: list[Path]) -> list[PayloadFile]:
        iso_files = [p for p in extracted if p.suffix.lower() == ".iso" and p.exists()]
        if iso_files:
            return payload_files_from_paths([iso_files[0]], "iso")

        bin_files = sorted(
            [p for p in extracted if p.suffix.lower() == ".bin" and p.exists()],
            key=ExtractWorkflow._track_sort_key,
        )
        return payload_files_from_paths(bin_files, "bin")

    @staticmethod
    def _find_first_bin_from_cue(cue_path: Path) -> Path | None:
        for path in ExtractWorkflow._bin_files_from_cue(cue_path):
            return path
        return None

    @staticmethod
    def _looks_like_cue_descriptor(path: Path) -> bool:
        if not path.exists() or path.stat().st_size > 16 * 1024:
            return False
        try:
            snippet = path.read_text(encoding="utf-8", errors="ignore")[:2048].lower()
        except OSError:
            return False
        markers = ["file \"", "track ", "datafile \"", "cd_rom"]
        return any(marker in snippet for marker in markers)

    _bin_files_from_cue = staticmethod(bin_files_from_cue)
