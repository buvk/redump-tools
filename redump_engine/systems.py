"""Console identifiers shared by manifests, DATs, and library action rules."""
from __future__ import annotations

import json
import re
from pathlib import Path

SYSTEM_NAMES = {
    "psx": "Sony - PlayStation",
    "ps2": "Sony - PlayStation 2",
    "dc": "Sega - Dreamcast",
    "xbox": "Microsoft - Xbox",
    "xbox360": "Microsoft - Xbox 360",
    "gamecube": "Nintendo - GameCube",
    "wii": "Nintendo - Nintendo Wii",
}


SHORT_SYSTEM_NAMES = {"psx": "PlayStation", "ps2": "PlayStation 2", "dc": "Dreamcast", "xbox": "Xbox",
                      "xbox360": "Xbox 360", "gamecube": "GameCube", "wii": "Wii"}


def recognize_system(name: str) -> str | None:
    name = name.casefold().strip()
    aliases = {"ps1": "psx", "psx": "psx", "ps2": "ps2", "dc": "dc", "dreamcast": "dc", "xbox": "xbox",
               "gamecube": "gamecube", "wii": "wii", "xbox360": "xbox360"}
    if name in aliases:
        return aliases[name]
    # Longer names must win: PlayStation 2 is not PlayStation, Xbox 360 is not Xbox.
    names = [*SYSTEM_NAMES.items(), ("wii", "Nintendo - Wii")]
    for key, title in sorted(names, key=lambda pair: len(pair[1]), reverse=True):
        if name == title.casefold() or re.fullmatch(re.escape(title.casefold()) + r"\s+\([^)]*\)", name):
            return key
    return None


def system_from_path(path: Path) -> str | None:
    for parent in path.parents:
        system = recognize_system(parent.name)
        if system:
            return system
    return None


def refresh_manifest_systems(root: Path, dry_run: bool = False) -> list[tuple[Path, str, str]]:
    """Update metadata only, retaining unknown fields and all original checksum records."""
    results: list[tuple[Path, str, str]] = []
    for path in sorted(root.rglob("*.json")):
        if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name.lower() != "manifest.json" and not path.name.lower().endswith(".manifest.json"):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            results.append((path, "Skipped", "Manifest points outside the selected library"))
            continue
        temporary = path.with_name("." + path.name + ".system.tmp")
        try:
            system = system_from_path(path)
            if system is None:
                results.append((path, "Skipped", "No recognized system folder"))
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or not {"title", "set_type", "discs"}.issubset(data):
                raise ValueError("Unsupported manifest schema")
            existing = data.get("system")
            if existing == system:
                results.append((path, "Skipped", f"Already set to {SYSTEM_NAMES[system]}"))
                continue
            if existing and (not isinstance(existing, str) or (recognize_system(existing) or existing) != system):
                raise ValueError(f"Existing system {existing!r} conflicts with folder system {system!r}")
            if not dry_run:
                data["system"] = system
                handle = temporary.open("x", encoding="utf-8")
                try:
                    with handle:
                        json.dump(data, handle, indent=2)
                        handle.write("\n")
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            results.append((path, "Planned" if dry_run else "Updated", f"System: {SYSTEM_NAMES[system]}"))
        except (OSError, ValueError) as exc:
            results.append((path, "Failed", str(exc)))
    return results
