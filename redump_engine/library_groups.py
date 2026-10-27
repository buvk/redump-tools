"""Read-only game grouping; group rows never become workflow inputs."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .filenames import dat_folder_name, disc_title, game_key, parse_disc_number
from .game_manifests import disc_for_source
from .library_models import LibraryItem
from .metadata import Manifest, load_manifest


@dataclass(frozen=True)
class GameGroup:
    id: str
    title: str
    indices: tuple[int, ...]
    numbers: dict[int, int]
    total_discs: int


def group_games(items: list[LibraryItem]) -> list[GameGroup]:
    candidates: dict[tuple[str, str], list[tuple[int, str, int, int]]] = {}
    manifests: dict[str, Manifest | None] = {}
    for index, item in enumerate(items):
        if item.kind == "Manifest":
            continue
        title = dat_folder_name(disc_title(item.path))
        number = parse_disc_number(disc_title(item.path)) or 1
        total = number
        if item.manifest:
            key = str(item.manifest)
            if key not in manifests:
                try:
                    manifests[key] = load_manifest(item.manifest)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    manifests[key] = None
            manifest = manifests[key]
            disc = disc_for_source(item.path, manifest) if manifest else None
            if manifest and disc:
                title, number = dat_folder_name(manifest.title), disc.disc
                total = max(total, manifest.disc_count)
        # An unknown console is grouped only within its current folder.
        scope = item.system or str(item.path.parent)
        identity = scope, game_key(title) or title.casefold()
        candidates.setdefault(identity, []).append((index, title, number, total))
    groups = []
    for identity, entries in candidates.items():
        numbers = {index: number for index, _, number, _ in entries}
        total = max(total for _, _, _, total in entries)
        if total <= 1 and len(set(numbers.values())) <= 1:
            continue
        group_id = "game:" + hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:24]
        groups.append(GameGroup(group_id, entries[0][1], tuple(numbers), numbers, total))
    return groups
