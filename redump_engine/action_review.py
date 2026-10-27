"""Plain-language conversion summaries shared by desktop implementations."""
from pathlib import Path

from .actions import ActionSpec


def conversion_review(action: ActionSpec, files: list[Path], keep_sources: bool, force: bool) -> str:
    output = {"convert": "CHD", "extract": "ISO or BIN/CUE", "xiso": "Xbox XISO",
              "rvz": "RVZ", "rvz-extract": "ISO"}[action.id]
    lines = [f"Selected: {len(files)} disc images", f"Output: {output}",
             "Sources: kept" if keep_sources else "Sources: deleted only after successful processing",
             "Existing outputs: may be overwritten" if force and action.allows_overwrite else "Existing outputs: kept"]
    if action.id == "xiso":
        lines.append("XISO removes disc data. The original ISO cannot be reconstructed from the XISO.")
    return "\n".join(lines)



def display_paths(paths: list[Path], full_paths: bool = False) -> list[str]:
    if full_paths:
        return list(map(str, paths))
    result = []
    for path in paths:
        depth = 1
        while depth < len(path.parts) and any(other != path and tuple(part.casefold() for part in other.parts[-depth:]) ==
                tuple(part.casefold() for part in path.parts[-depth:]) for other in paths):
            depth += 1
        result.append(str(Path(*path.parts[-depth:])))
    return result
