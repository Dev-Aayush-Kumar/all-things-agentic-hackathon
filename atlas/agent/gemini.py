"""Gemini model configuration helpers.

Hackathon requirement: Gemini 3.5 or newer. ATLAS never silently replaces
the configured model with an older one.
"""

from __future__ import annotations

import re

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
MIN_GEMINI_VERSION = (3, 5)

_VERSION_RE = re.compile(
    r"^gemini-(?:(?P<major>\d+)\.(?P<minor>\d+)|(?P<maj_only>\d+))(?P<rest>.*)?$",
    re.IGNORECASE,
)


def parse_gemini_version(model: str) -> tuple[int, int] | None:
    """Return (major, minor) for names like gemini-3.5-flash, or None if unknown."""
    text = (model or "").strip()
    if text.lower().startswith("models/"):
        text = text.split("/", 1)[1]
    match = _VERSION_RE.match(text)
    if not match:
        return None
    if match.group("major") is not None:
        return int(match.group("major")), int(match.group("minor"))
    # gemini-3-flash-preview → treat as 3.0
    return int(match.group("maj_only")), 0


def gemini_meets_minimum(model: str) -> bool:
    """Whether the configured model is Gemini 3.5 or newer."""
    version = parse_gemini_version(model)
    if version is None:
        return False
    return version >= MIN_GEMINI_VERSION
