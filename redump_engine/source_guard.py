"""Detect changed conversion inputs before publishing output or deleting originals."""
import hashlib
from pathlib import Path


class SourceGuard:
    def __init__(self, paths: list[Path]):
        self.expected = {path: self._signature(path) for path in paths}
        self.digests = {path: self._digest(path) for path in paths}
        self.check()

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int, int]:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Source is not a regular file: {path}")
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

    @staticmethod
    def _digest(path: Path) -> bytes:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").digest()

    def check(self, *, content: bool = False) -> None:
        for path, expected in self.expected.items():
            if self._signature(path) != expected or content and self._digest(path) != self.digests[path]:
                raise ValueError(f"Source changed during conversion; original kept: {path}")

    def remap(self, source: Path, target: Path) -> None:
        self.digests = {target / path.relative_to(source) if path.is_relative_to(source) else path: value for path, value in self.digests.items()}
        self.expected = {target / path.relative_to(source) if path.is_relative_to(source) else path: value
                         for path, value in self.expected.items()}

    def remap_paths(self, changes: dict[Path, Path]) -> None:
        self.digests = {changes.get(path, path): value for path, value in self.digests.items()}
        self.expected = {changes.get(path, path): value for path, value in self.expected.items()}

    def transferred(self, source: Path, target: Path, *, copied: bool) -> None:
        if source not in self.expected:
            return
        if self._signature(source) != self.expected[source]:
            raise ValueError(f"Source changed during conversion; original kept: {source}")
        target_signature = self._signature(target)
        if not copied and target_signature != self.expected[source]:
            raise ValueError(f"Transferred source changed; original kept: {source}")
        if copied:
            if self._digest(source) != self.digests[source] or self._digest(target) != self.digests[source]:
                raise ValueError(f"Transferred data differs from original; source kept: {source}")
            if self._signature(source) != self.expected[source]:
                raise ValueError(f"Source changed during transfer; original kept: {source}")
        if self._signature(target) != target_signature:
            raise ValueError(f"Transferred data changed during validation: {target}")
        self.expected[target] = target_signature
        self.digests[target] = self.digests.pop(source)
        del self.expected[source]
