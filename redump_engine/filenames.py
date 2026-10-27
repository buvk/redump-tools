from __future__ import annotations

import re
from pathlib import Path

DISC_PATTERN = re.compile(r"\(disc\s*(\d+)\)", re.IGNORECASE)
REV_PATTERN = re.compile(r"\(rev[^)]*\)", re.IGNORECASE)


def safe_name(name: str) -> str:
    """Return a valid Windows file component or reject an empty name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("Cannot determine a valid output name")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name


def game_key(title: str) -> str:
    return normalize_name(dat_folder_name(title))


def is_xiso(path: Path) -> bool:
    return path.name.lower().endswith(".xiso.iso") or path.suffix.lower() == ".xiso"


def disc_title(path: Path) -> str:
    return path.name[:-len(".xiso.iso")] if path.name.lower().endswith(".xiso.iso") else path.stem


def normalize_name(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"[^a-z0-9]+", "", lowered)


def parse_disc_number(name: str) -> int | None:
    matches = DISC_PATTERN.findall(name)
    if not matches:
        return None
    return int(matches[-1])


def dat_output_stem(dat_name: str, disc_number: int | None = None, total_discs: int = 1) -> str:
    if total_discs <= 1:
        return dat_name
    if disc_number is None:
        return dat_name
    if f"(Disc {disc_number})".lower() in dat_name.lower():
        return dat_name

    # Keep revision tags trailing the disc marker: "Game (Disc 1) (Rev 1)".
    rev_match = REV_PATTERN.search(dat_name)
    if rev_match:
        head = dat_name[: rev_match.start()].rstrip()
        tail = dat_name[rev_match.start() :].lstrip()
        if head:
            return f"{head} (Disc {disc_number}) {tail}"

    return f"{dat_name} (Disc {disc_number})"


def rename_if_needed(path: Path, desired_name: str, dry_run: bool = False) -> Path:
    desired_path = path.with_name(desired_name)
    if path.name == desired_name:
        return path
    if desired_path.exists():
        # Keep existing destination and preserve source path to avoid destructive collisions.
        return path
    if dry_run:
        return desired_path
    path.rename(desired_path)
    return desired_path


def dat_folder_name(dat_name: str) -> str:
    if not DISC_PATTERN.search(dat_name):
        return dat_name

    # Remove only the disc marker and preserve any trailing qualifiers
    # like revision tags: "Game (Disc 1) (Rev 1)" -> "Game (Rev 1)".
    cleaned = DISC_PATTERN.sub("", dat_name)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned
