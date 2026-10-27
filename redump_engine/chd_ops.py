from __future__ import annotations

import codecs
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import cast

from .events import ToolProgress


class ChdmanError(RuntimeError):
    pass


@dataclass
class ChdInfo:
    logical_size: int | None
    track_count: int
    has_cd_metadata: bool
    has_dvd_metadata: bool
    raw_output: str
    has_gd_metadata: bool = False


class Chdman:
    def __init__(self, executable: Path, dry_run: bool = False, force: bool = False, progress: bool = False,
                 on_progress: Callable[[ToolProgress], None] | None = None):
        self.executable = executable
        self.dry_run = dry_run
        self.force = force
        self.progress = progress
        self.on_progress = on_progress

    def _run(self, *args: str) -> str:
        cmd = [str(self.executable), *args]
        if self.dry_run:
            return "DRY_RUN: " + " ".join(cmd)

        if self.progress and args and args[0] != "info":
            print(f"[chdman] {args[0]} ...", flush=True)
        started = time.monotonic()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if args and args[0] != "info" and (self.progress or self.on_progress):
            output, returncode = self._stream(cmd)
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
            output, returncode = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
        if returncode != 0:
            raise ChdmanError(f"Command failed: {' '.join(cmd)}\n{output}")
        if self.progress and args and args[0] != "info":
            print(f"[chdman] {args[0]} finished in {time.monotonic() - started:.1f}s", flush=True)
        return output

    def _stream(self, cmd: list[str]) -> tuple[str, int]:
        operation = cmd[1]
        source = Path(cmd[cmd.index("-i") + 1]) if "-i" in cmd else None

        def report(percent: float | None, message: str, state: str) -> None:
            if self.on_progress:
                self.on_progress(ToolProgress(operation, source, percent, message, state))

        last_percent: float | None = None

        def consume(line: str) -> None:
            nonlocal last_percent
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
            if not line:
                return
            match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", line)
            if match and 0 <= float(match[1]) <= 100:
                last_percent = float(match[1])
                report(last_percent, line, "running")
            if self.progress:
                print(f"[chdman] {line}", flush=True)

        report(None, "Starting chdman", "started")
        chunks: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags) as process:
                assert process.stdout is not None
                stream = cast(BufferedReader, process.stdout)
                while chunk := stream.read1(4096):
                    text = decoder.decode(chunk)
                    chunks.append(text)
                    lines = re.split(r"[\r\n]", pending + text)
                    pending = lines.pop()
                    for line in lines:
                        consume(line)
                tail = decoder.decode(b"", final=True)
                chunks.append(tail)
                consume(pending + tail)
                returncode = process.wait()
        except Exception:
            report(last_percent, "Unable to run chdman", "failed")
            raise
        report(100.0 if returncode == 0 else last_percent,
               "Finished" if returncode == 0 else f"chdman failed (exit {returncode})",
               "finished" if returncode == 0 else "failed")
        return "".join(chunks), returncode

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
        track_tags = ("CHT2", "CHTR", "CHGD", "CHGT")
        track_count = max(len(re.findall(fr"Tag='{tag}'", output, flags=re.IGNORECASE)) for tag in track_tags)
        has_gd_metadata = bool(re.search(r"Tag='(?:CHGD|CHGT)'", output, flags=re.IGNORECASE))
        has_cd_metadata = track_count > 0
        has_dvd_metadata = bool(re.search(r"Tag='DVD\s*'", output, flags=re.IGNORECASE))
        return ChdInfo(
            logical_size=logical_size,
            track_count=track_count,
            has_cd_metadata=has_cd_metadata,
            has_dvd_metadata=has_dvd_metadata,
            raw_output=output,
            has_gd_metadata=has_gd_metadata,
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
