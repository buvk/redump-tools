from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .filenames import dat_folder_name, normalize_name
from .metadata import (
    DatIndex,
    ManifestDisc,
    compute_hashes,
    load_manifest,
    match_dat_entry,
    payload_checksums_from_payload_files,
)
from .payload_matching import complete_matches
from .payload_paths import resolve_payload
from .systems import system_from_path


def _empty_details() -> list[str]:
    return []


@dataclass
class VerifyResult:
    game_dir: Path
    manifest_path: Path
    status: str
    dat_name: str
    matched_dat_name: str | None
    details: list[str] = field(default_factory=_empty_details)


class VerifyWorkflow:
    def __init__(self, dat_index: DatIndex | None, verbose: bool = False, rehash: bool = False):
        self.dat_index = dat_index
        self.verbose = verbose
        self.rehash = rehash

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[verbose][verify] {message}")

    def _rehash_problems(self, game_dir: Path, disc: ManifestDisc) -> list[str]:
        """Compare files on disk with the checksums recorded in the manifest."""
        problems: list[str] = []
        for payload in disc.payload_files:
            name = str(payload.get("name", ""))
            try:
                path = resolve_payload(game_dir, name)
            except ValueError as error:
                problems.append(f"disc {disc.disc}: {error}")
                continue
            if not name or not path.is_file():
                problems.append(f"disc {disc.disc}: payload file missing on disk: {name or '<unnamed>'}")
                continue
            print(f"[verify] Hashing original file: {path}", flush=True)
            sha1, crc32 = compute_hashes(path)
            if sha1 != str(payload.get("sha1", "")).lower() or crc32 != str(payload.get("crc32", "")).lower():
                problems.append(f"disc {disc.disc}: checksum mismatch on disk: {name}")
        return problems

    def process_game_dir(self, game_dir: Path, manifest_path: Path | None = None) -> VerifyResult:
        manifest_path = manifest_path or game_dir / "manifest.json"
        manifest = load_manifest(manifest_path)
        if manifest is None:
            return VerifyResult(
                game_dir=game_dir,
                manifest_path=manifest_path,
                status="skipped",
                dat_name="",
                matched_dat_name=None,
                details=["manifest.json not found"],
            )

        if self.dat_index is None:
            return VerifyResult(
                game_dir=game_dir,
                manifest_path=manifest_path,
                status="failed",
                dat_name=manifest.dat_name,
                matched_dat_name=None,
                details=["DAT index is unavailable; ensure --dats points to valid DAT files"],
            )

        if not manifest.disc_entries:
            return VerifyResult(
                game_dir=game_dir,
                manifest_path=manifest_path,
                status="failed",
                dat_name=manifest.dat_name,
                matched_dat_name=None,
                details=["manifest has no payload checksums"],
            )

        details: list[str] = []
        matched_names: list[str] = []
        notes: list[str] = []

        for disc in manifest.disc_entries:
            if self.rehash:
                problems = self._rehash_problems(game_dir, disc)
                if problems:
                    return VerifyResult(
                        game_dir=game_dir,
                        manifest_path=manifest_path,
                        status="failed",
                        dat_name=manifest.dat_name,
                        matched_dat_name=None,
                        details=problems,
                    )
            sha1s, crc32s = payload_checksums_from_payload_files(disc.payload_files)
            if not sha1s and not crc32s:
                details.append(f"disc {disc.disc}: missing payload checksums")
                return VerifyResult(
                    game_dir=game_dir,
                    manifest_path=manifest_path,
                    status="failed",
                    dat_name=manifest.dat_name,
                    matched_dat_name=None,
                    details=details,
                )

            if manifest.disc_count > 1:
                expected_hint = f"{manifest.dat_name} (Disc {disc.disc})"
            else:
                expected_hint = manifest.dat_name
            expected = match_dat_entry(expected_hint, self.dat_index) if expected_hint else None
            candidates = complete_matches(self.dat_index, disc.payload_files, manifest.system or system_from_path(game_dir))
            unique = {(entry.system, entry.name) for entry in candidates}
            matched = candidates[0] if len(unique) == 1 else None

            self._log(
                f"game={game_dir.name}, disc={disc.disc}, dat_name={manifest.dat_name or 'none'}, "
                f"sha1_count={len(sha1s)}, crc_count={len(crc32s)}, "
                f"name_match={expected.name if expected else 'none'}, "
                f"checksum_match={matched.name if matched else 'none'}"
            )

            if matched is None:
                details.append(f"disc {disc.disc}: no unique DAT entry matched the complete payload list (track count, sizes and checksums)")
                return VerifyResult(
                    game_dir=game_dir,
                    manifest_path=manifest_path,
                    status="failed",
                    dat_name=manifest.dat_name,
                    matched_dat_name=None,
                    details=details,
                )

            if expected is not None and normalize_name(expected.name) != normalize_name(matched.name):
                notes.append(
                    f"disc {disc.disc}: name differs from checksum DAT entry: "
                    f"name='{expected.name}' checksum='{matched.name}'"
                )

            matched_names.append(matched.name)

        if not matched_names:
            return VerifyResult(
                game_dir=game_dir,
                manifest_path=manifest_path,
                status="failed",
                dat_name=manifest.dat_name,
                matched_dat_name=None,
                details=["manifest has no payload checksums"],
            )

        unique_matched = sorted(set(matched_names))
        if manifest.dat_name:
            manifest_base = normalize_name(dat_folder_name(manifest.dat_name))
            for matched_name in unique_matched:
                if normalize_name(dat_folder_name(matched_name)) != manifest_base:
                    notes.append(
                        "manifest dat_name/disc DAT mismatch: "
                        f"manifest='{manifest.dat_name}' matched_disc='{matched_name}'"
                    )

        if self.rehash:
            count = sum(len(disc.payload_files) for disc in manifest.disc_entries)
            details.append(f"Read and rehashed {count} original ISO/BIN files from disk; SHA-1 and CRC32 match the manifest")
        details.append("all manifest discs matched DAT payload checksums")
        details.extend(notes)
        return VerifyResult(
            game_dir=game_dir,
            manifest_path=manifest_path,
            status="passed",
            dat_name=manifest.dat_name,
            matched_dat_name=", ".join(unique_matched),
            details=details,
        )
