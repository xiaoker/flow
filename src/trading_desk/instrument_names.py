"""Small, source-backed instrument-name fallbacks.

This module is intentionally *not* a security master.  The official HKEX
top-10 file can occasionally publish a valid code with an empty name.  Keep a
tiny exception list for those cases, preserve the source value separately,
and let the quality report surface anything that remains unresolved.
"""

from __future__ import annotations

import math
from typing import Any


# HKEX's southbound top-10 file omitted the name for 00100 on some recent
# dates.  HKEX's official security/disclosure material identifies it as
# MINIMAX-W.  The key includes direction because the same numeric code can
# have different semantics across channels.
HKEX_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    ("southbound", "00100"): "MINIMAX-W",
}


def clean_instrument_name(value: Any) -> str | None:
    """Return a trimmed source name, preserving an empty source as ``None``."""

    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def resolve_hkex_instrument_name(
    *, code: str, direction: str, raw_name: Any
) -> tuple[str | None, str]:
    """Resolve an HKEX display name without mutating the raw source value.

    Returns ``(name, source)`` where source is one of ``source``, ``override``
    or ``unresolved``.  Unknown codes deliberately remain unresolved instead
    of being guessed.
    """

    source_name = clean_instrument_name(raw_name)
    if source_name:
        return source_name, "source"
    normalized_code = str(code).strip().zfill(5)
    fallback = HKEX_NAME_OVERRIDES.get((str(direction), normalized_code))
    if fallback:
        return fallback, "override"
    return None, "unresolved"


def display_instrument_name(*, code: str, direction: str, raw_name: Any) -> str:
    """Return a safe UI label for a code whose official name may be absent."""

    resolved, _ = resolve_hkex_instrument_name(
        code=code, direction=direction, raw_name=raw_name
    )
    return resolved or f"{str(code).strip()}｜名称待确认"
