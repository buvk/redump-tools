"""Shared directory discovery and safe iteration for all game workflows."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .events import EventReporter

_SKIPPED_DIRS = {"dats", "cuesheets", "tools", "redump_engine", "scripts", "tests", "reports", "__pycache__"}


def _iter_game_dirs(root: Path) -> list[Path]:
    game_dirs: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.lower() in _SKIPPED_DIRS:
            continue
        # --root may be a platform folder (children are games) or a library (children are platforms).
        if _looks_like_single_game_dir(child):
            game_dirs.append(child)
            continue
        for game in sorted(child.iterdir()):
            if game.is_dir():
                game_dirs.append(game)

    if not game_dirs and _looks_like_single_game_dir(root):
        print(f"[warn] {root} is itself a game folder; use --game-dir to process it")
    return game_dirs


def _looks_like_single_game_dir(path: Path) -> bool:
    if (path / "manifest.json").exists():
        return True
    patterns = ("*.chd", "*.rvz", "*.cue", "*.iso", "*.bin")
    return any(any(path.glob(pattern)) for pattern in patterns)


def _expand_game_dir(target: Path) -> list[Path]:
    if not target.exists() or not target.is_dir():
        return [target]
    if _looks_like_single_game_dir(target):
        return [target]

    children = [child for child in sorted(target.iterdir()) if child.is_dir()]
    if children:
        return children
    return [target]



def iter_batch(game_dirs: list[Path], stop_file: Path | None, verbose: bool, events: EventReporter) -> Iterator[Path]:
    for current, game_dir in enumerate(game_dirs, 1):
        if stop_file and stop_file.exists():
            print("[batch] stopped after current game", flush=True)
            events.emit("stopped")
            break
        if verbose and not events.enabled:
            print("[progress] " + json.dumps({"current": current, "total": len(game_dirs), "game_dir": str(game_dir.resolve())}), flush=True)
        events.emit("progress", current=current, total=len(game_dirs), folder=str(game_dir.resolve()))
        yield game_dir
