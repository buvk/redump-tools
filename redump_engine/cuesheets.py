from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from .filenames import normalize_name


@dataclass
class CueHit:
    zip_path: Path
    member: str


class CueRepository:
    def __init__(self, cues_root: Path):
        self.cues_root = cues_root
        self._index: dict[str, CueHit] | None = None

    def _build_index(self) -> dict[str, CueHit]:
        index: dict[str, CueHit] = {}
        if not self.cues_root.exists():
            return index

        for item in self.cues_root.iterdir():
            if item.is_file() and item.suffix.lower() == ".zip":
                with ZipFile(item) as archive:
                    for member in archive.namelist():
                        if not member.lower().endswith(".cue"):
                            continue
                        key = normalize_name(Path(member).stem)
                        index[key] = CueHit(zip_path=item, member=member)
            elif item.is_file() and item.suffix.lower() == ".cue":
                key = normalize_name(item.stem)
                index[key] = CueHit(zip_path=item, member=item.name)
        return index

    @property
    def index(self) -> dict[str, CueHit]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def find(self, dat_name: str) -> CueHit | None:
        key = normalize_name(dat_name)
        if key in self.index:
            return self.index[key]

        for known, hit in self.index.items():
            if known.startswith(key) or key.startswith(known):
                return hit
        return None

    def copy_trusted_cue(self, dat_name: str, destination: Path, expected_bin_name: str | None = None) -> bool:
        hit = self.find(dat_name)
        if hit is None:
            return False

        if hit.zip_path.suffix.lower() == ".zip":
            with ZipFile(hit.zip_path) as archive:
                raw = archive.read(hit.member)
        else:
            raw = hit.zip_path.read_bytes()

        if not expected_bin_name:
            destination.write_bytes(raw)
            return True

        content = raw.decode("utf-8", errors="ignore")
        newline = "\r\n" if b"\r\n" in raw else "\n"
        content = _retarget_first_file_line(content, expected_bin_name, newline)
        destination.write_bytes(content.encode("utf-8"))
        return True


def _retarget_first_file_line(cue_text: str, expected_bin_name: str, newline: str) -> str:
    lines = cue_text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped.startswith("file "):
            lines[idx] = f'FILE "{expected_bin_name}" BINARY'
            break
    return newline.join(lines) + newline
