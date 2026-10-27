"""Library records with compatibility helpers for callers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .filenames import disc_title


@dataclass(frozen=True)
class LibraryItem:
    path: Path
    kind: str
    size: int
    manifest: Path | None
    dependencies: tuple[Path, ...] = ()
    problem: str = ""
    original_size: int | None = None
    missing_record: bool = True
    system: str | None = None
    title: str = ""
    original_id: tuple[str, str, int] | None = None

    @property
    def game_title(self) -> str:
        return self.title or disc_title(self.path)

    @property
    def saved_bytes(self) -> int | None:
        return self.original_size - self.size if self.kind in {"CHD", "RVZ", "XISO"} and self.original_size is not None else None

    @property
    def saved_percent(self) -> float | None:
        if self.original_size is None or self.original_size <= 0 or self.saved_bytes is None:
            return None
        return 100 * self.saved_bytes / self.original_size

    def eligible(self, operation: str, check_chds: bool = False) -> bool:
        from .library_rules import eligible
        return eligible(self, operation, check_chds)
