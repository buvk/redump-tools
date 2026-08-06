from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen


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


ASSETS: tuple[RedumpAsset, ...] = (
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
            destination.parent.mkdir(parents=True, exist_ok=True)
            _download_to_path(asset.url, destination)

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


def _download_to_path(url: str, destination: Path) -> None:
    request = Request(
        url,
        headers={
            "User-Agent": "redump-tools-workflow/0.1",
            "Accept": "application/zip, application/octet-stream;q=0.9, */*;q=0.1",
        },
    )
    with urlopen(request) as response:
        with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, suffix=".tmp") as handle:
            temp_path = Path(handle.name)
            shutil.copyfileobj(response, handle)

    temp_path.replace(destination)