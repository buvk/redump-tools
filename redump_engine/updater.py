from __future__ import annotations

import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from .dat_loader import parse_dat_xml

DOWNLOAD_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class RedumpAsset:
    system: str
    kind: str
    url: str
    target_name: str

    @property
    def destination_dir_name(self) -> str:
        return "dats" if self.kind == "dat" else "cuesheets"


@dataclass(frozen=True)
class UpdateResult:
    system: str
    kind: str
    url: str
    destination: Path
    changed: bool
    error: str | None = None

ASSETS: tuple[RedumpAsset, ...] = (
    RedumpAsset(system="xbox", kind="dat", url="https://redump.info/datfile/XBOX", target_name="redump_xbox_dat.zip"),
    RedumpAsset(system="gamecube", kind="dat", url="https://redump.info/datfile/GC", target_name="redump_gamecube_dat.zip"),
    RedumpAsset(system="wii", kind="dat", url="https://redump.info/datfile/WII", target_name="redump_wii_dat.zip"),
    RedumpAsset(system="dc", kind="dat", url="https://redump.info/datfile/DC", target_name="redump_dc_dat.zip"),
    RedumpAsset(system="dc", kind="cue", url="https://redump.info/cues/DC", target_name="redump_dc_cuesheets.zip"),
    RedumpAsset(system="psx", kind="dat", url="https://redump.info/datfile/PSX", target_name="redump_psx_dat.zip"),
    RedumpAsset(system="psx", kind="cue", url="https://redump.info/cues/PSX", target_name="redump_psx_cuesheets.zip"),
    RedumpAsset(system="ps2", kind="dat", url="https://redump.info/datfile/PS2", target_name="redump_ps2_dat.zip"),
    RedumpAsset(system="ps2", kind="cue", url="https://redump.info/cues/PS2", target_name="redump_ps2_cuesheets.zip"),
)


def available_systems() -> tuple[str, ...]:
    return tuple(sorted({asset.system for asset in ASSETS}))


def selected_assets(
    systems: list[str] | tuple[str, ...] | None = None,
    *,
    include_dats: bool = True,
    include_cuesheets: bool = True,
) -> list[RedumpAsset]:
    wanted_systems = {value.lower() for value in systems} if systems else set(available_systems())
    assets: list[RedumpAsset] = []
    for asset in ASSETS:
        if asset.system not in wanted_systems:
            continue
        if asset.kind == "dat" and not include_dats:
            continue
        if asset.kind == "cue" and not include_cuesheets:
            continue
        assets.append(asset)
    return assets


def update_assets(
    *,
    dats_dir: Path,
    cuesheets_dir: Path,
    systems: list[str] | tuple[str, ...] | None = None,
    include_dats: bool = True,
    include_cuesheets: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[UpdateResult]:
    results: list[UpdateResult] = []
    for asset in selected_assets(systems, include_dats=include_dats, include_cuesheets=include_cuesheets):
        destination_root = dats_dir if asset.destination_dir_name == "dats" else cuesheets_dir
        destination = (destination_root / asset.target_name).resolve()
        if verbose or dry_run:
            action = "would download" if dry_run else "downloading"
            print(f"[update] {action} {asset.system} {asset.kind}: {asset.url} -> {destination}")

        if not dry_run:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                _download_to_path(asset.url, destination, kind=asset.kind, system=asset.system)
            except (OSError, ValueError, BadZipFile) as exc:
                results.append(
                    UpdateResult(
                        system=asset.system,
                        kind=asset.kind,
                        url=asset.url,
                        destination=destination,
                        changed=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

        results.append(
            UpdateResult(
                system=asset.system,
                kind=asset.kind,
                url=asset.url,
                destination=destination,
                changed=not dry_run,
            )
        )

    return results


def _download_to_path(url: str, destination: Path, *, kind: str, system: str) -> None:
    request = Request(
        url,
        headers={
            "User-Agent": "redump-tools-workflow/0.1",
            "Accept": "application/zip, application/octet-stream;q=0.9, */*;q=0.1",
        },
    )
    temp_path: Path | None = None
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, suffix=".tmp") as handle:
                temp_path = Path(handle.name)
                shutil.copyfileobj(response, handle)

        # Never replace a good archive with an HTML error page or truncated download.
        with ZipFile(temp_path) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise BadZipFile(f"corrupt member in downloaded archive: {bad_member}")
            _validate_download(archive, kind=kind, system=system)

        temp_path.replace(destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _validate_download(archive: ZipFile, *, kind: str, system: str) -> None:
    suffixes = (".dat", ".xml") if kind == "dat" else (".cue",)
    members = [member for member in archive.infolist() if not member.is_dir() and member.filename.lower().endswith(suffixes)]
    if not members:
        raise ValueError(f"Downloaded archive contains no {kind} files; previous download kept")
    for member in members:
        text = archive.read(member).decode("utf-8-sig")
        if kind == "cue":
            if not re.search(r'(?im)^\s*(?:FILE|DATAFILE)\s+"[^"]+"', text) or not re.search(r"(?im)^\s*TRACK\s+\d+\s+\S+", text):
                raise ValueError(f"Invalid downloaded cuesheet: {member.filename}")
            continue
        try:
            entries = parse_dat_xml(text)
        except (ET.ParseError, ValueError) as error:
            raise ValueError(f"Invalid downloaded DAT {member.filename}: {error}") from error
        if not entries:
            raise ValueError(f"Downloaded DAT contains no game entries: {member.filename}")
        for entry in entries:
            if entry.system and entry.system != system:
                raise ValueError(f"Downloaded DAT identifies {entry.system}, expected {system}: {member.filename}")
            if not entry.roms:
                raise ValueError(f"Downloaded DAT game has no ROM records: {entry.name}")
            for rom in entry.roms:
                if not rom.name or rom.size is None or rom.size < 0 or not (rom.sha1 or rom.crc):
                    raise ValueError(f"Incomplete downloaded DAT ROM record: {entry.name} / {rom.name}")
                if (rom.sha1 is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", rom.sha1)
                        or rom.crc is not None and not re.fullmatch(r"[0-9a-fA-F]{8}", rom.crc)):
                    raise ValueError(f"Invalid downloaded DAT checksum: {entry.name} / {rom.name}")
