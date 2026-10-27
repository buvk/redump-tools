"""Plan playlist path changes without modifying files."""
from __future__ import annotations

import os
from pathlib import Path


def playlist_updates(folder: Path, target: Path, outputs: dict[Path, Path], *, relocate_folder: bool = True) -> dict[Path, bytes]:
    """Keep local and system-level playlists valid when file/folder names change."""
    changes = {}
    source_map = {source.resolve(): output for source, output in outputs.items()}
    for playlist in {*folder.glob("*.m3u"), *folder.parent.glob("*.m3u")}:
        if playlist.is_symlink():
            continue
        raw = playlist.read_bytes()
        text = raw.decode("utf-8-sig")
        lines = text.splitlines(keepends=True)
        updated = []
        for line in lines:
            name = line.strip()
            if not name or name.startswith("#"):
                updated.append(line)
                continue
            reference = Path(name)
            resolved = (playlist.parent / reference).resolve()
            replacement = source_map.get(resolved, resolved)
            if relocate_folder and replacement.is_relative_to(folder):
                replacement = target / replacement.relative_to(folder)
            if replacement == resolved:
                updated.append(line)
                continue
            destination = source_map.get(playlist.resolve(), playlist)
            if relocate_folder and playlist.is_relative_to(folder):
                destination = target / playlist.relative_to(folder)
            base = destination.parent
            entry = str(replacement) if reference.is_absolute() else os.path.relpath(replacement, base).replace("\\", "/")
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            updated.append(entry + ending)
        replacement_text = "".join(updated)
        if replacement_text != text:
            changes[playlist] = replacement_text.encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    return changes
