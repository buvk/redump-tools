"""Report naming/layout differences separately from payload checksum validity."""
from __future__ import annotations

from pathlib import Path

from .filenames import safe_name
from .game_folders import contains_one_game
from .game_manifests import disc_for_source
from .library_metadata import manifest_for
from .metadata import DatIndex, Manifest
from .naming_analysis import analyze_manifest_names


def manifest_layout_warnings(path: Path, manifest: Manifest, dat_index: DatIndex | None) -> list[str]:
    if dat_index is None:
        return []
    folder = path.parent
    analysis = analyze_manifest_names(manifest, dat_index)
    matched = analysis.entries
    if not matched:
        return []
    warnings = []
    titles = analysis.titles
    if len(titles) == 1:
        expected = safe_name(next(iter(titles)))
        if folder.name.casefold() != expected.casefold():
            warnings.append(f"Game folder name: '{folder.name}'; Redump name: '{expected}'.")
    else:
        warnings.append("This manifest contains different game titles; use one game per folder.")
    dedicated = contains_one_game(folder, [manifest.title, *titles])
    if path.name != "manifest.json":
        if dedicated:
            warnings.append(f"Manifest filename: '{path.name}'; expected 'manifest.json' in this game folder.")
        else:
            warnings.append("Named manifest is in a shared folder; organize the game before using 'manifest.json'.")
    for image in sorted(folder.iterdir()):
        if not image.is_file() or image.is_symlink() or image.suffix.lower() not in {".chd", ".rvz", ".iso", ".xiso", ".cue"}:
            continue
        if manifest_for(image) != path:
            continue
        disc = disc_for_source(image, manifest)
        entry = matched.get(disc.disc) if disc else None
        if entry is None:
            continue
        assert disc is not None
        expected = analysis.image_name(image, disc.disc)
        if image.name.casefold() != expected.casefold():
            warnings.append(f"Image filename: '{image.name}'; Redump name: '{expected}'.")
    return warnings
