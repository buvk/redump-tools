"""Short status labels that preserve the kind of verification performed."""
from __future__ import annotations


def verification_badge(value: str) -> tuple[str, str]:
    mode, _, status = value.partition(":")
    if status.strip() == "Passed":
        label = {"CHD": "CHD checked", "Original files": "Original DAT match", "Original manifest": "Original DAT match",
                 "Disc image": "Image DAT match", "Image integrity": "Image checked"}.get(mode, "DAT match")
        return "✓ " + label, "Success"
    if status.strip() == "Failed":
        return "! Failed", "Error"
    if status.strip() == "Warning":
        return "! Names/folders need attention", "Warning"
    return "○ Not checked", "Neutral"


def result_badge(status: str) -> str:
    return {"Passed": "✓ Verified", "Completed": "✓ Completed", "Updated": "✓ Updated", "Warning": "! Needs attention",
            "Failed": "! Failed", "Skipped": "— Skipped", "Planned": "○ Preview"}.get(status, status)
