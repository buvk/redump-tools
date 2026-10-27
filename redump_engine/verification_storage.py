"""Compact on-disk verification records with shared DAT fingerprints."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .json_storage import read_object, write_object
from .paths import SETTINGS_PATH, USER_DATA_ROOT
from .verification_evidence import checks_for, evidence_record


def pack_records(records: dict[str, Any]) -> dict[str, Any]:
    contexts: dict[str, Any] = {}
    identifiers: dict[str, str] = {}
    def pack(record):
        if not isinstance(record, dict):
            return record
        result = dict(record)
        if isinstance(record.get("checks"), dict):
            result["checks"] = {mode: pack(check) for mode, check in record["checks"].items()}
        if isinstance(record.get("inputs"), dict) and "dats" in record["inputs"]:
            inputs = dict(record["inputs"])
            dats = inputs.pop("dats")
            key = json.dumps(dats, sort_keys=True, separators=(",", ":"))
            if key not in identifiers:
                identifier = str(len(contexts) + 1)
                identifiers[key] = identifier
                contexts[identifier] = dats
            inputs["dats_ref"] = identifiers[key]
            result["inputs"] = inputs
        return result
    packed = {path: pack(record) for path, record in records.items()}

    return {"version": 2, "dat_fingerprints": contexts, "records": packed}


def unpack_records(value: dict[str, Any]) -> dict[str, Any] | None:
    records = value.get("records")
    if not isinstance(records, dict):
        return None
    if value.get("version") == 1:
        return records
    contexts = value.get("dat_fingerprints")
    if value.get("version") != 2 or not isinstance(contexts, dict):
        return None
    def unpack(record):
        if not isinstance(record, dict):
            return record
        result = dict(record)
        if isinstance(record.get("checks"), dict):
            result["checks"] = {mode: value for mode, check in record["checks"].items() if (value := unpack(check)) is not None}
        if isinstance(record.get("inputs"), dict) and "dats_ref" in record["inputs"]:
            inputs = dict(record["inputs"])
            identifier = inputs.pop("dats_ref")
            if not isinstance(identifier, str) or identifier not in contexts:
                return evidence_record(checks_for(result)) if isinstance(result.get("checks"), dict) else None
            inputs["dats"] = contexts[identifier]
            result["inputs"] = inputs
        return result
    return {path: result for path, record in records.items() if (result := unpack(record)) is not None}


def verification_cache_path(settings_path: Path = SETTINGS_PATH) -> Path:
    if settings_path.resolve() == SETTINGS_PATH.resolve():
        return USER_DATA_ROOT / "cache" / "verification.json"
    return settings_path.with_name(settings_path.stem + ".verification-cache.json")


def load_verification_records(path: Path) -> dict[str, Any]:
    return unpack_records(read_object(path)) or {}


def save_verification_records(records: dict[str, Any], path: Path) -> None:
    packed = pack_records(records)
    if read_object(path) != packed:
        write_object(path, packed, compact=True)
