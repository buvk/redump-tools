from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class ChdmanError(RuntimeError):
    pass


@dataclass
class ChdInfo:
    logical_size: int | None
    track_count: int
    has_cd_metadata: bool
    has_dvd_metadata: bool
    raw_output: str


class Chdman:
    def __init__(self, executable: Path, dry_run: bool = False, force: bool = False, progress: bool = False):
        self.executable = executable
        self.dry_run = dry_run
        self.force = force
        self.progress = progress

    def _run(self, *args: str) -> str:
        cmd = [str(self.executable), *args]
        if self.dry_run:
            return "DRY_RUN: " + " ".join(cmd)

        if self.progress and args and args[0] != "info":
            print(f"[chdman] {args[0]} ...", flush=True)
        started = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise ChdmanError(f"Command failed: {' '.join(cmd)}\n{output}")
        if self.progress and args and args[0] != "info":
            print(f"[chdman] {args[0]} finished in {time.monotonic() - started:.1f}s", flush=True)
        return output

    def _force_flags(self, output: Path) -> list[str]:
        """Refuse to clobber an existing output unless force is enabled."""
        if self.dry_run:
            return []
        if output.exists() and not self.force:
            raise ChdmanError(f"Refusing to overwrite existing file (use --force): {output}")
        return ["-f"] if self.force else []

    def verify(self, input_chd: Path) -> str:
        return self._run("verify", "-i", str(input_chd))

    def info(self, input_chd: Path) -> ChdInfo:
        output = self._run("info", "-i", str(input_chd))
        match = re.search(r"Logical size:\s*([0-9,]+)", output, flags=re.IGNORECASE)
        logical_size = int(match.group(1).replace(",", "")) if match else None
        # CHT2 metadata entries map to per-track descriptors in CD CHDs.
        track_count = len(re.findall(r"Tag='CHT2'", output, flags=re.IGNORECASE))
        has_cd_metadata = bool(re.search(r"Tag='CHT2'", output, flags=re.IGNORECASE))
        has_dvd_metadata = bool(re.search(r"Tag='DVD\s*'", output, flags=re.IGNORECASE))
        return ChdInfo(
            logical_size=logical_size,
            track_count=track_count,
            has_cd_metadata=has_cd_metadata,
            has_dvd_metadata=has_dvd_metadata,
            raw_output=output,
        )

    def extractcd(self, input_chd: Path, output_file: Path) -> str:
        return self._run("extractcd", *self._force_flags(output_file), "-i", str(input_chd), "-o", str(output_file))

    def extractcd_to_iso(self, input_chd: Path, output_iso: Path) -> str:
        # For CD CHDs that contain ISO payloads, chdman requires -ob for the binary output.
        # -f is always needed here because the cue output goes to the null device.
        self._force_flags(output_iso)
        null_sink = "NUL" if os.name == "nt" else "/dev/null"
        return self._run(
            "extractcd",
            "-f",
            "-i",
            str(input_chd),
            "-o",
            null_sink,
            "-ob",
            str(output_iso),
        )

    def extractdvd(self, input_chd: Path, output_file: Path) -> str:
        return self._run("extractdvd", *self._force_flags(output_file), "-i", str(input_chd), "-o", str(output_file))

    def splitbin(self, input_chd: Path, output_cue: Path) -> str:
        # chdman 0.267 exposes split output as extractcd --splitbin (-sb).
        return self._run("extractcd", *self._force_flags(output_cue), "-sb", "-i", str(input_chd), "-o", str(output_cue))

    def createcd(self, input_image: Path, output_chd: Path) -> str:
        return self._run("createcd", *self._force_flags(output_chd), "-i", str(input_image), "-o", str(output_chd))

    def createdvd(self, input_image: Path, output_chd: Path) -> str:
        return self._run("createdvd", *self._force_flags(output_chd), "-i", str(input_image), "-o", str(output_chd))
