"""Independent verification proofs and consistent strongest-valid selection."""
from typing import Any

MODE_STRENGTH = {"image": 5, "originals": 4, "manifest": 3, "image-integrity": 2, "chd": 2}


def checks_for(record: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(record, dict):
        return {}
    nested = record.get("checks")
    if isinstance(nested, dict):
        return {mode: dict(check) for mode, check in nested.items() if isinstance(mode, str) and isinstance(check, dict) and check.get("mode") == mode
                and isinstance(check.get("label"), str) and isinstance(check.get("inputs"), dict)}
    mode = record.get("mode")
    return {mode: {key: value for key, value in record.items() if key != "checks"}} if isinstance(mode, str) else {}


def evidence_record(checks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not checks:
        return None
    def strength(check):
        label = str(check.get("label", ""))
        severity = 3 if label.endswith(": Failed") else 2 if label.endswith(": Warning") else 1
        mode = check.get("mode")
        return severity, MODE_STRENGTH.get(mode, 0) if isinstance(mode, str) else 0
    preferred = max(checks.values(), key=strength)
    return {**preferred, "checks": checks}
