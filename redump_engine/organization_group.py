"""Combine changes sharing one game manifest into a single reviewed transaction."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .cue import cue_reference_root, referenced_bin_files
from .playlists import playlist_updates
from .workflow_layout import LayoutPlan, cue_reference_update


def _merge_manifest(contents: list[bytes]) -> bytes:
    result: dict[str, Any] = {}
    discs: dict[int, dict[str, Any]] = {}
    for content in contents:
        value = json.loads(content)
        for key, field in value.items():
            if key == "discs":
                continue
            if key in result and result[key] != field:
                raise ValueError(f"Conflicting shared manifest field: {key}")
            result[key] = copy.deepcopy(field)
        for disc in value.get("discs", []):
            number = int(disc["disc"])
            if number not in discs:
                discs[number] = copy.deepcopy(disc)
                continue
            previous = discs[number]
            for key, field in disc.items():
                if key == "source_files":
                    previous[key] = list(dict.fromkeys([*previous.get(key, []), *field]))
                elif key in previous and previous[key] != field:
                    raise ValueError(f"Conflicting disc {number} field: {key}")
                else:
                    previous[key] = copy.deepcopy(field)
    result["discs"] = [discs[number] for number in sorted(discs)]
    return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def combine_game_plans(plans: list[LayoutPlan], source_manifest: Path, moved_discs: set[int]) -> LayoutPlan:
    if not plans:
        raise ValueError("No game images are available for organization")
    first = plans[0]
    if len(plans) == 1:
        return first
    moves: dict[Path, Path] = {}
    writes: dict[Path, bytes] = {}
    fingerprints = {}
    directories = {}
    obsolete = set()
    copies = set()
    manifests = []
    for plan in plans:
        if (plan.source_folder, plan.folder, plan.manifest, plan.title) != (first.source_folder, first.folder, first.manifest, first.title):
            raise ValueError("Shared manifest images disagree about their game folder")
        for source, target in plan.moves.items():
            if source in moves and moves[source] != target:
                raise ValueError(f"Shared file has conflicting destinations: {source}")
            if source not in moves and target in moves.values():
                raise ValueError(f"Two game files would overwrite the same destination: {target}")
            moves[source] = target
        for path, content in plan.writes.items():
            if path == first.manifest:
                manifests.append(content)
            elif path != source_manifest and path.suffix.lower() not in {".cue", ".m3u"}:
                if path in writes and writes[path] != content:
                    raise ValueError(f"Conflicting shared file update: {path}")
                writes[path] = content
        fingerprints.update(plan.fingerprints)
        directories.update(plan.directories)
        obsolete.update(plan.obsolete)
        copies.update(plan.copies)
    if manifests:
        writes[first.manifest] = _merge_manifest(manifests)
    if source_manifest != first.manifest:
        original = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
        remaining = [disc for disc in original.get("discs", []) if int(disc["disc"]) not in moved_discs]
        if remaining:
            original["discs"] = remaining
            writes[source_manifest] = (json.dumps(original, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            obsolete.discard(source_manifest)
        else:
            obsolete.add(source_manifest)
    # Recompute reference edits once using every move in the game.
    for old, new in moves.items():
        if old.suffix.lower() == ".cue":
            content = cue_reference_update(old, new, moves)
            if content is not None:
                writes[new] = content
    writes.update(playlist_updates(first.source_folder, first.source_folder, moves))
    unselected_tracks = referenced_bin_files(cue_reference_root(first.source_folder), moves)
    copies = {path for path in copies if path.resolve() in unselected_tracks}
    return LayoutPlan(first.source_folder, first.folder, first.manifest, moves, writes, sorted(obsolete),
                      fingerprints, directories, first.title, copies)
