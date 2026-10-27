"""Rollback file changes in reverse order when a workflow fails.

Metadata snapshots stay in memory; potentially large output backups use disk.
Only explicitly registered paths are restored or removed.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Literal

from .file_io import write_metadata

if TYPE_CHECKING:
    from .source_guard import SourceGuard


class FileTransaction:
    def __init__(self) -> None:
        self._undo: list[Callable[[], object]] = []
        self._watched: set[Path] = set()
        self._backups: list[Path] = []
        self._created_dirs: list[Path] = []
        self._cleanup_dirs: set[Path] = set()

    def __enter__(self) -> FileTransaction:
        return self

    def watch_metadata(self, paths: Iterable[Path]) -> None:
        for path in paths:
            if path in self._watched:
                continue
            if path.is_symlink():
                raise ValueError(f"Cannot modify a symbolic link: {path}")
            content = path.read_bytes() if path.exists() else None
            self._undo.append(lambda path=path, content=content: self._restore(path, content))
            self._watched.add(path)

    @staticmethod
    def _restore(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            write_metadata(path, content)

    def watch_output(self, path: Path) -> None:
        """Register before an external tool can create a partial output."""
        if path in self._watched:
            return
        if path.is_symlink():
            raise ValueError(f"Cannot replace a symbolic link: {path}")
        if path.exists():
            # Use an independent disk copy: a tool may truncate the existing file.
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".rollback_", delete=False) as handle:
                backup = Path(handle.name)
            self._backups.append(backup)
            shutil.copy2(path, backup)
            self._undo.append(lambda: backup.replace(path))
        else:
            self._undo.append(lambda: path.unlink(missing_ok=True))
        self._watched.add(path)

    def prepare_output(self, path: Path) -> None:
        """Back up and detach an output so tools cannot modify hard-linked aliases."""
        self.watch_output(path)
        if path.exists():
            path.unlink()

    def is_backup(self, path: Path) -> bool:
        return path in self._backups

    def remove_empty_folder_after_commit(self, folder: Path) -> None:
        """Retry source-folder cleanup once rollback backups have been removed."""
        self._cleanup_dirs.add(folder)

    def mkdir(self, folder: Path) -> None:
        if folder.exists():
            return
        folder.mkdir()
        self._created_dirs.append(folder)

    def transfer(self, source: Path, target: Path, *, copy: bool = False, source_guard: SourceGuard | None = None) -> None:
        """Publish exclusively, retaining shared sources when requested."""
        state = {"moved": False}
        if not copy:
            try:
                os.link(source, target)
            except FileExistsError:
                raise
            except OSError:
                pass  # Filesystems without hard links use exclusive copy.
            else:
                self._undo.append(lambda: self._undo_transfer(source, target, copy=False, moved=state["moved"]))
                if source_guard is not None:
                    source_guard.transferred(source, target, copied=False)
                source.unlink()
                state["moved"] = True
                return
        # Register cleanup immediately, so a partial copy is removed on failure.
        with target.open("xb") as outgoing:
            self._undo.append(lambda: self._undo_transfer(source, target, copy=copy, moved=state["moved"]))
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, outgoing)
        if not copy:
            shutil.copystat(source, target)
            if source_guard is not None:
                source_guard.transferred(source, target, copied=True)
            source.unlink()
            state["moved"] = True

    @staticmethod
    def _undo_transfer(source: Path, target: Path, *, copy: bool, moved: bool = False) -> None:
        if moved:
            if source.exists() or source.is_symlink():
                raise FileExistsError(f"Rollback destination is occupied: {source}. Original file retained at: {target}")
            target.rename(source)
        else:
            target.unlink(missing_ok=True)

    def rename_folder(self, source: Path, target: Path) -> None:
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Destination appeared during operation: {target}")
        source.rename(target)
        self._remap_backups(source, target)

        def restore_folder() -> None:
            target.rename(source)
            self._remap_backups(target, source)

        self._undo.append(restore_folder)

    def _remap_backups(self, source: Path, target: Path) -> None:
        self._backups = [target / path.relative_to(source) if path.is_relative_to(source) else path
                         for path in self._backups]

    def remove_empty_folder(self, folder: Path) -> None:
        folder.rmdir()
        self._undo.append(folder.mkdir)

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> Literal[False]:
        failures: list[BaseException] = []
        if exc is not None:
            for undo in reversed(self._undo):
                try:
                    undo()
                except BaseException as failure:
                    failures.append(failure)
            for folder in reversed(self._created_dirs):
                try:
                    folder.rmdir()
                except OSError as failure:
                    failures.append(failure)
        # Retain backups when restoration failed; the error names the paths.
        if not failures:
            for backup in self._backups:
                try:
                    backup.unlink(missing_ok=True)
                except OSError as failure:
                    warnings.warn(f"Changes completed, but a rollback backup could not be removed: {backup}: {failure}",
                                  RuntimeWarning, stacklevel=2)
        if exc is None:
            for folder in sorted(self._cleanup_dirs, key=lambda path: len(path.parts), reverse=True):
                try:
                    if not folder.is_symlink() and folder.is_dir() and not any(folder.iterdir()):
                        folder.rmdir()
                except OSError as failure:
                    warnings.warn(f"Changes completed, but the empty source folder could not be removed: {folder}: {failure}",
                                  RuntimeWarning, stacklevel=2)
        if failures:
            assert exc is not None
            raise BaseExceptionGroup("Operation failed and some changes could not be restored:\n" + "\n".join(map(str, failures)), [exc, *failures])
        return False
