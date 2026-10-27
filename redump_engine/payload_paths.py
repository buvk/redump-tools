"""Resolve payload paths consistently inside a game folder."""
from pathlib import Path


def resolve_payload(folder: Path, name: str) -> Path:
    relative = Path(name)
    if not name or relative.is_absolute():
        raise ValueError(f"Payload must use a relative path: {name}")
    base = folder.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Payload escapes the game folder: {name}")
    return path


def payload_name(path: Path, folder: Path | None = None) -> str:
    if folder is None:
        return path.name
    relative = path.resolve().relative_to(folder.resolve()).as_posix()
    resolve_payload(folder, relative)
    return relative
