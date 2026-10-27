"""DolphinTool conversions with stage events and isolated temporary user data."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .events import ToolProgress


@dataclass(frozen=True)
class DolphinVerification:
    sha1: str
    crc32: str
    problems: bool
    report: str


def parse_verification(report: str) -> DolphinVerification:
    def field(pattern: str) -> str:
        match = re.search(pattern, report, re.MULTILINE | re.IGNORECASE)
        if not match:
            raise ValueError("DolphinTool returned an incomplete verification report")
        return match[1].lower()
    return DolphinVerification(field(r"^SHA1:\s*([0-9a-f]{40})\s*$"),
                              field(r"^CRC32:\s*([0-9a-f]{8})\s*$"),
                              field(r"^Problems Found:\s*(Yes|No)\s*$") == "yes", report.strip())


class DolphinTool:
    def __init__(self, executable: Path, on_progress: Callable[[ToolProgress], None] | None = None):
        self.executable = executable
        self.on_progress = on_progress

    def verify(self, source: Path) -> DolphinVerification:
        if not self.executable.is_file():
            raise FileNotFoundError(f"DolphinTool is missing: {self.executable}")
        operation = "Verifying disc image"
        before = source.stat()
        if self.on_progress:
            self.on_progress(ToolProgress(operation, source, None, "Reading image with DolphinTool", "started"))
        try:
            with tempfile.TemporaryDirectory(prefix="redump_dolphin_verify_") as user:
                result = subprocess.run([str(self.executable), "verify", "-u", user, "-i", str(source)],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.stdout:
                print(result.stdout, flush=True)
            if result.returncode:
                raise RuntimeError(f"DolphinTool verify failed (exit {result.returncode}): {result.stdout.strip()}")
            report = parse_verification(result.stdout)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Image changed during verification; run the check again")
        except Exception:
            if self.on_progress:
                self.on_progress(ToolProgress(operation, source, None, "Image verification failed", "failed"))
            raise
        if self.on_progress:
            self.on_progress(ToolProgress(operation, source, 100, "Finished", "finished"))
        return report

    def convert(self, source: Path, destination: Path, *, rvz: bool) -> None:
        if not self.executable.is_file():
            raise FileNotFoundError(f"DolphinTool is missing: {self.executable}. Select DolphinTool.exe in Settings.")
        operation = "Compressing RVZ" if rvz else "Restoring ISO"
        if self.on_progress:
            self.on_progress(ToolProgress(operation, source, None, "DolphinTool is working", "started"))
        print(f"[dolphin] {operation}: {source.name}", flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix="redump_dolphin_") as user:
                command = [str(self.executable), "convert", "-u", user, "-i", str(source), "-o", str(destination),
                           "-f", "rvz" if rvz else "iso"]
                if rvz:
                    command.extend(("-b", "131072", "-c", "zstd", "-l", "5"))
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, errors="replace", creationflags=flags)
            if result.stdout:
                print(result.stdout, flush=True)
            if result.returncode:
                raise RuntimeError(f"DolphinTool failed (exit {result.returncode}): {result.stdout.strip()}")
            if not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError("DolphinTool did not produce a nonempty output image")
        except Exception:
            if self.on_progress:
                self.on_progress(ToolProgress(operation, source, None, "DolphinTool failed", "failed"))
            raise
        if self.on_progress:
            self.on_progress(ToolProgress(operation, source, 100, "Finished", "finished"))
