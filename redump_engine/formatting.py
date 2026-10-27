"""Shared display formatting without UI dependencies."""
from __future__ import annotations

import re


def abbreviate_timezone(timestamp: str) -> str:
    """Normalize verbose Windows timezone names, including saved timestamps."""
    zones = {"Pacific": ("PST", "PDT"), "Mountain": ("MST", "MDT"), "Central": ("CST", "CDT"),
             "Eastern": ("EST", "EDT"), "Alaskan": ("AKST", "AKDT"), "Hawaiian": ("HST", "HDT")}
    pattern = r"\b(" + "|".join(zones) + r") (Standard|Daylight|Summer) Time\b"
    return re.sub(pattern, lambda match: zones[match[1]][match[2] != "Standard"], timestamp)


def format_size(size: int) -> str:
    for unit, divisor in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if abs(size) >= divisor:
            return f"{size / divisor:,.2f} {unit}"
    return f"{size:,} B"
