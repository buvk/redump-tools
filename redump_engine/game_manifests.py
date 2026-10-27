"""Choose and merge metadata for a game, including numbered disc sets."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .filenames import dat_folder_name, disc_title, normalize_name, parse_disc_number
from .filenames import game_key as game_key
from .game_folders import contains_one_game, is_game_folder
from .metadata import Manifest, ManifestDisc, load_manifest, save_manifest


def disc_for_source(source: Path, manifest: Manifest) -> ManifestDisc | None:
    number = parse_disc_number(source.stem)
    if number is not None:
        matches = [disc for disc in manifest.disc_entries if disc.disc == number]
    else:
        matches = [disc for disc in manifest.disc_entries if source.name.casefold() in {name.casefold() for name in disc.source_files} or any(
            normalize_name(Path(str(payload.get("name", ""))).stem) == normalize_name(source.stem)
            for payload in disc.payload_files)]
        if not matches and len(manifest.disc_entries) == 1:
            matches = manifest.disc_entries
    return matches[0] if len(matches) == 1 else None


def manifest_destination(source: Path, title: str | None = None) -> Path:
    title = dat_folder_name(title or disc_title(source))
    key = game_key(title)
    generic = source.parent / "manifest.json"
    dedicated = contains_one_game(source.parent, [title], [source])
    if generic.exists():
        try:
            existing = load_manifest(generic)
            if existing and game_key(existing.title) == key and dedicated:
                return generic
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        # An unrelated or unreadable folder manifest must be preserved.
        return source.parent / f"{title}.manifest.json"
    if dedicated and is_game_folder(source.parent, title):
        return generic
    return source.parent / f"{title}.manifest.json"


def _payload_identity(disc: ManifestDisc) -> Counter | None:
    """Compare original contents without depending on payload filenames."""
    values = []
    for payload in disc.payload_files:
        kind, size = str(payload.get("kind", "")).casefold(), payload.get("size")
        sha1, crc = str(payload.get("sha1") or "").casefold(), str(payload.get("crc32") or "").casefold()
        if kind not in {"iso", "bin"} or type(size) is not int or size < 0 or not (sha1 or crc):
            return None
        values.append((kind, size, sha1, crc))
    return Counter(values) if values else None


def merge_game_manifest(destination: Path, incoming: Manifest) -> None:
    """Preserve other discs and consolidate compatible legacy sidecars atomically."""
    key = game_key(incoming.title)
    existing_discs = {}
    owners: dict[int, Path] = {}
    obsolete: list[Path] = []
    system = incoming.system
    candidates = [*sorted(destination.parent.glob("*.manifest.json")), destination.parent / "manifest.json"]
    # Prefer canonical payload names only after checking existing records for
    # compatibility; a canonical file must never hide a conflicting sidecar.
    candidates = [path for path in dict.fromkeys(candidates) if path != destination] + [destination]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            existing = load_manifest(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            if path == destination:
                raise ValueError(f"Cannot replace unreadable game manifest: {path}") from None
            continue
        if existing is None:
            continue
        if game_key(existing.title) != key:
            if path == destination:
                raise ValueError(f"Manifest belongs to a different game: {path}")
            continue
        if system and existing.system and system != existing.system:
            raise ValueError(f"Conflicting manifest systems: {system} and {existing.system}")
        system = system or existing.system
        for disc in existing.disc_entries:
            previous = existing_discs.get(disc.disc)
            if previous is not None:
                identity = _payload_identity(disc)
                compatible = previous.payload_files == disc.payload_files or identity is not None and identity == _payload_identity(previous)
                if not compatible:
                    raise ValueError(f"Conflicting manifests for disc {disc.disc}: {owners[disc.disc].name} and {path.name}; resolve before updating {destination.name}")
                sources = [*disc.source_files, *previous.source_files] if path == destination else [*previous.source_files, *disc.source_files]
                disc = ManifestDisc(disc.disc, disc.payload_files, list(dict.fromkeys(sources)))
            existing_discs[disc.disc] = disc
            owners[disc.disc] = path
        if path != destination:
            obsolete.append(path)
    for disc in incoming.disc_entries:
        previous = existing_discs.get(disc.disc)
        identity = _payload_identity(disc)
        if previous is not None and identity is not None and identity == _payload_identity(previous):
            disc = ManifestDisc(disc.disc, disc.payload_files, list(dict.fromkeys([*previous.source_files, *disc.source_files])))
        existing_discs[disc.disc] = disc
    discs = sorted(existing_discs.values(), key=lambda disc: disc.disc)
    kinds = {str(payload.get("kind", "")).lower() for disc in discs for payload in disc.payload_files}
    set_type = "mixed" if {"iso", "bin"}.issubset(kinds) else "iso" if "iso" in kinds else incoming.set_type
    merged = Manifest(incoming.schema_version, set_type, dat_folder_name(incoming.title), discs, system)
    save_manifest(destination, merged)
    for path in obsolete:
        path.unlink(missing_ok=True)
