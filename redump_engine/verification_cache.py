"""Remember completed checks, expiring them when their inputs change."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import VerificationMode
from .dat_cache import source_signature
from .events import ResultRecord
from .file_identity import file_fingerprint
from .library_metadata import is_manifest, manifest_for
from .metadata import load_manifest
from .payload_paths import resolve_payload
from .verification_evidence import checks_for, evidence_record
from .verification_results import cache_label


def fingerprint(path: Path) -> list[int] | None:
    try:
        return file_fingerprint(path)
    except OSError:
        return None


def signature(path: Path, mode: VerificationMode, dats: Path) -> dict[str, Any] | None:
    try:
        own = fingerprint(path)
        if own is None:
            return None
        value: dict[str, Any] = {"file": own, "evidence": 2}
        if mode in {VerificationMode.MANIFEST, VerificationMode.ORIGINALS, VerificationMode.IMAGE}:
            value["rules"] = 3
            value["dats"] = [str(dats.resolve()), source_signature(dats)]
            # Naming/layout warnings and original-file checks depend on the
            # surrounding files too; no image data is read here.
            if mode != VerificationMode.IMAGE:
                value["siblings"] = [[p.name, fingerprint(p)] for p in sorted(path.parent.iterdir()) if p.is_file()]
                metadata = path if is_manifest(path) else manifest_for(path)
                manifest = None
                if metadata is not None:
                    try:
                        manifest = load_manifest(metadata)
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if manifest is not None and metadata is not None:
                    payloads = sorted({str(payload.get("name", "")) for disc in manifest.disc_entries for payload in disc.payload_files})
                    value["payloads"] = [[name, fingerprint(resolve_payload(metadata.parent, name))] for name in payloads]

        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


class VerificationRecords:
    """JSON-compatible records; filesystem checks belong on a worker thread."""
    def __init__(self, records: object = None):
        self.records: dict[str, Any] = dict(records) if isinstance(records, dict) else {}

    def valid_checks(self, path: Path, dats: Path, record: Any = None, current: dict | None = None) -> dict[str, dict[str, Any]]:
        snapshots = current if current is not None else {}
        checks = checks_for(self.records.get(str(path.resolve())) if record is None else record)
        valid = {}
        for key, check in checks.items():
            try:
                mode = VerificationMode(check["mode"])
                if mode not in snapshots:
                    snapshots[mode] = signature(path, mode, dats)
                if isinstance(check.get("label"), str) and check.get("inputs") == snapshots[mode] and snapshots[mode] is not None:
                    valid[key] = check
            except (KeyError, TypeError, ValueError):
                continue
        return valid

    def remember(self, path: Path, mode: VerificationMode, label: str, dats: Path) -> None:
        inputs = signature(path, mode, dats)
        if inputs is not None:
            checks = self.valid_checks(path, dats, current={mode: inputs})
            checks[mode.value] = {"mode": mode.value, "label": label, "inputs": inputs}
            self.records[str(path.resolve())] = evidence_record(checks)

    def lookup(self, path: Path, dats: Path) -> str | None:
        record = evidence_record(self.valid_checks(path, dats))
        return record["label"] if record else None


@dataclass(frozen=True)
class VerificationJob:
    version: int
    path: Path
    mode: VerificationMode | None
    label: str
    dats: Path
    record: Any
    checked_inputs: dict[str, Any] | None = None
    updates: dict[VerificationMode, tuple[str, dict[str, Any] | None]] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationUpdate:
    job: VerificationJob
    record: Any
    label: str | None


class VerificationCache(VerificationRecords):
    """Main-thread labels backed by a coalescing filesystem worker."""
    def __init__(self, records: object = None):
        super().__init__(records)
        self.labels: dict[Path, str] = {}
        self._jobs: queue.Queue[Path | None] = queue.Queue()
        self._results: queue.Queue[VerificationUpdate] = queue.Queue()
        self._pending: dict[Path, VerificationJob] = {}
        self._lock = threading.Lock()
        self._versions: dict[Path, int] = {}
        self._remembering: set[Path] = set()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._worker_records: dict[Path, Any] = {}

    @property
    def saving(self) -> bool:
        return bool(self._remembering)

    def remember_result(self, result: ResultRecord, mode: VerificationMode, dats: Path) -> None:
        """Update the checked image and, for manifest checks, its shared record."""
        if result.source is None or mode == VerificationMode.NONE:
            return
        targets = {result.source}
        if mode == VerificationMode.MANIFEST and result.reference:
            targets.add(result.reference)
        for path in targets:
            checked = (result.verified_inputs or {}).get(str(path.resolve()))
            if checked is not None:
                self.request(path, dats, mode, cache_label(result, mode), checked_inputs=checked)

    def request(self, path: Path, dats: Path, mode: VerificationMode | None = None, label: str = "", *, checked_inputs: dict[str, Any] | None = None) -> None:
        """Queue one latest request per path without doing filesystem work."""
        with self._lock:
            if self._closed or mode is None and path in self._remembering:
                return
            if mode is not None:
                self._remembering.add(path)
            version = self._versions.get(path, 0) + 1
            self._versions[path] = version
            pending = self._pending.get(path)
            updates = dict(pending.updates) if pending else {}
            if mode is not None:
                updates[mode] = (label, checked_inputs)
            job = VerificationJob(version, path, mode, label, dats, self.records.get(str(path)), checked_inputs, updates)
            if path not in self._pending:
                self._jobs.put(path)
            self._pending[path] = job
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while (path := self._jobs.get()) is not None:
            with self._lock:
                job = self._pending.pop(path, None)
            if job is None:
                continue
            try:
                checks = checks_for(job.record)
                checks.update(checks_for(self._worker_records.get(path)))
                current = {}
                for mode, (label, checked) in job.updates.items():
                    current[mode] = signature(path, mode, job.dats)
                    inputs = checked if checked is not None else current[mode]
                    if inputs is not None and inputs == current[mode]:
                        checks[mode.value] = {"mode": mode.value, "label": label, "inputs": inputs}
                store = VerificationRecords()
                combined = evidence_record(checks)
                valid = store.valid_checks(path, job.dats, combined or {}, current)
                record = evidence_record(valid)
                label = record["label"] if record else None
                self._worker_records[path] = record
                self._results.put(VerificationUpdate(job, record, label))
            except Exception:
                # A failed lookup invalidates this proof, but must never stop the
                # worker or leave the desktop waiting forever for a save.
                self._worker_records[path] = None
                self._results.put(VerificationUpdate(job, None, None))

    def drain(self) -> tuple[bool, bool]:
        """Apply completed work on the UI thread; return display/save changes."""
        changed = saved = False
        while True:
            try:
                update = self._results.get_nowait()
            except queue.Empty:
                return changed, saved
            job, record, label = update.job, update.record, update.label
            if job.version != self._versions.get(job.path):
                continue
            self._remembering.discard(job.path)
            changed |= self.labels.get(job.path) != label
            if label:
                self.labels[job.path] = label
            else:
                self.labels.pop(job.path, None)
            if isinstance(record, dict):
                if self.records.get(str(job.path)) != record:
                    self.records[str(job.path)] = record
                    saved = True
            elif self.records.pop(str(job.path), None) is not None:
                saved = True

    def close(self) -> None:
        """Stop accepting jobs and release the worker without blocking the UI."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._jobs.put(None)
