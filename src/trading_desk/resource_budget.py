"""Pure D1/R2 resource-budget calculations used by offline audits.

This module intentionally has no network or database side effects.  A later
workflow can feed it Cloudflare Analytics numbers before deciding whether an
optional stage may run.  Keeping the decision pure makes the quota guard easy
to test without spending production rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DECISIONS = {"allow", "warn", "defer_optional", "block_d1_bulk"}


@dataclass(frozen=True)
class ResourceLimits:
    d1_rows_read_per_day: int
    d1_rows_written_per_day: int
    d1_storage_bytes: int
    target_rows_read_per_day: int
    target_rows_written_per_day: int
    hard_rows_read_per_day: int
    hard_rows_written_per_day: int
    hard_storage_bytes: int

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ResourceLimits":
        limits = payload.get("limits", payload)
        targets = payload.get("targets", {})
        hard = payload.get("hardLimits", {})
        storage = payload.get("storage", {})
        return cls(
            d1_rows_read_per_day=_positive_int(limits, "d1RowsReadPerDay"),
            d1_rows_written_per_day=_positive_int(limits, "d1RowsWrittenPerDay"),
            d1_storage_bytes=_positive_int(storage, "d1Bytes"),
            target_rows_read_per_day=_positive_int(targets, "d1RowsReadPerDay"),
            target_rows_written_per_day=_positive_int(targets, "d1RowsWrittenPerDay"),
            hard_rows_read_per_day=_positive_int(hard, "d1RowsReadPerDay"),
            hard_rows_written_per_day=_positive_int(hard, "d1RowsWrittenPerDay"),
            hard_storage_bytes=_positive_int(storage, "hardD1Bytes"),
        )


@dataclass(frozen=True)
class ResourceUsage:
    rows_read: int = 0
    rows_written: int = 0
    storage_bytes: int = 0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ResourceUsage":
        if not payload:
            return cls()
        return cls(
            rows_read=_optional_nonnegative_int(
                payload, "rows_read", "rowsRead", default=0
            ),
            rows_written=_optional_nonnegative_int(
                payload, "rows_written", "rowsWritten", default=0
            ),
            storage_bytes=_optional_nonnegative_int(
                payload, "storage_bytes", "storageBytes", default=0
            ),
        )

    def plus(self, other: "ResourceUsage") -> "ResourceUsage":
        return ResourceUsage(
            rows_read=self.rows_read + other.rows_read,
            rows_written=self.rows_written + other.rows_written,
            storage_bytes=max(self.storage_bytes, other.storage_bytes),
        )


@dataclass(frozen=True)
class ResourceEstimate:
    """An estimate for one stage before it performs D1 work.

    ``logical_rows`` is the number of table rows touched.  ``index_count``
    models the extra billed writes caused by maintaining indexes on those
    rows.  Callers may provide explicit billed values when a previous run has
    a better measurement.
    """

    rows_read: int
    rows_written: int
    storage_bytes: int = 0


def estimate_stage(
    *,
    logical_rows: int = 0,
    index_count: int = 0,
    rows_read: int | None = None,
    rows_written: int | None = None,
    storage_bytes: int = 0,
) -> ResourceEstimate:
    if logical_rows < 0 or index_count < 0 or storage_bytes < 0:
        raise ValueError("resource estimates must be non-negative")
    estimated_read = logical_rows if rows_read is None else rows_read
    estimated_write = (
        logical_rows * (1 + index_count)
        if rows_written is None
        else rows_written
    )
    if estimated_read < 0 or estimated_write < 0:
        raise ValueError("resource estimates must be non-negative")
    return ResourceEstimate(
        rows_read=estimated_read,
        rows_written=estimated_write,
        storage_bytes=storage_bytes,
    )


def decide(
    measured: ResourceUsage,
    estimate: ResourceEstimate,
    limits: ResourceLimits,
    *,
    optional: bool = True,
) -> dict[str, Any]:
    """Return a deterministic allow/defer decision and its evidence.

    The internal target is the normal operating envelope.  Crossing it emits
    ``warn``.  Crossing the internal hard gate defers optional work; no caller
    may use this function to silently drop required evidence.
    """

    projected = measured.plus(
        ResourceUsage(
            rows_read=estimate.rows_read,
            rows_written=estimate.rows_written,
            storage_bytes=estimate.storage_bytes,
        )
    )
    over_official = (
        projected.rows_read > limits.d1_rows_read_per_day
        or projected.rows_written > limits.d1_rows_written_per_day
        or projected.storage_bytes > limits.d1_storage_bytes
    )
    over_hard = (
        projected.rows_read > limits.hard_rows_read_per_day
        or projected.rows_written > limits.hard_rows_written_per_day
        or projected.storage_bytes > limits.hard_storage_bytes
    )
    over_target = (
        projected.rows_read > limits.target_rows_read_per_day
        or projected.rows_written > limits.target_rows_written_per_day
    )

    if over_official:
        decision = "block_d1_bulk"
        reason = "projected usage exceeds the official D1/storage limit"
    elif over_hard:
        decision = "defer_optional" if optional else "block_d1_bulk"
        reason = "projected usage exceeds the internal hard gate"
    elif over_target:
        decision = "warn"
        reason = "projected usage exceeds the normal operating target"
    else:
        decision = "allow"
        reason = "projected usage is inside the normal operating target"

    return {
        "decision": decision,
        "reason": reason,
        "optional": optional,
        "measured": {
            "rowsRead": measured.rows_read,
            "rowsWritten": measured.rows_written,
            "storageBytes": measured.storage_bytes,
        },
        "estimate": {
            "rowsRead": estimate.rows_read,
            "rowsWritten": estimate.rows_written,
            "storageBytes": estimate.storage_bytes,
        },
        "projected": {
            "rowsRead": projected.rows_read,
            "rowsWritten": projected.rows_written,
            "storageBytes": projected.storage_bytes,
        },
        "limits": {
            "officialRowsRead": limits.d1_rows_read_per_day,
            "officialRowsWritten": limits.d1_rows_written_per_day,
            "officialStorageBytes": limits.d1_storage_bytes,
            "targetRowsRead": limits.target_rows_read_per_day,
            "targetRowsWritten": limits.target_rows_written_per_day,
            "hardRowsRead": limits.hard_rows_read_per_day,
            "hardRowsWritten": limits.hard_rows_written_per_day,
            "hardStorageBytes": limits.hard_storage_bytes,
        },
    }


def _positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(payload: Mapping[str, Any], *keys: str) -> int:
    value: Any = None
    for key in keys:
        if key in payload:
            value = payload[key]
            break
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{keys[0]} must be a non-negative integer")
    return value


def _optional_nonnegative_int(
    payload: Mapping[str, Any], *keys: str, default: int = 0
) -> int:
    """Read an optional non-negative integer while keeping missing metrics zero."""

    if not any(key in payload for key in keys):
        return default
    return _nonnegative_int(payload, *keys)
