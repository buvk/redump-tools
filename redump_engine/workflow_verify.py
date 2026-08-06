from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .filenames import dat_folder_name, normalize_name
from .metadata import DatIndex, load_manifest, match_dat_entry, payload_checksums_from_payload_files


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
    def __init__(self, dat_index: DatIndex | None, verbose: bool = False):
        self.dat_index = dat_index
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[verbose][verify] {message}")

    def process_game_dir(self, game_dir: Path) -> VerifyResult:
        manifest_path = game_dir / "manifest.json"
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
            matched = self.dat_index.match_by_checksums(sha1s=sha1s, crc32s=crc32s)

            self._log(
                f"game={game_dir.name}, disc={disc.disc}, dat_name={manifest.dat_name or 'none'}, "
                f"sha1_count={len(sha1s)}, crc_count={len(crc32s)}, "
                f"name_match={expected.name if expected else 'none'}, "
                f"checksum_match={matched.name if matched else 'none'}"
            )

            if matched is None:
                details.append(f"disc {disc.disc}: no DAT entry matched payload checksums")
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
