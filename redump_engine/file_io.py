"""Publish small metadata files without changing hard-linked aliases."""
import tempfile
from pathlib import Path


def write_metadata(path: Path, content: bytes) -> int:
    """Replace the selected pathname atomically; callers own transaction rollback."""
    if path.is_symlink():
        raise ValueError(f"Cannot replace symbolic-link metadata: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".redump_metadata_", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
        return len(content)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
