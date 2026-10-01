"""Resource accounting and conservative admission contracts.

The v0.6-B contract deliberately keeps resource accounting separate from the
existing production workflows. It is a deterministic candidate-side model
that callers can feed with provider receipts later:

* every dimension carries ``value + unit + basis``;
* missing observations are ``unknown`` and never silently become zero;
* admission includes active reservations, so concurrent tasks cannot
  oversell the same quota;
* a timeout or unknown outcome keeps its reservation until a later, explicit
  reconciliation; and
* D1 ``rows_read``/``rows_written`` are physical provider counters, not a
  logical row estimate.

There are no D1, R2, GitHub or network calls in this module. The in-memory
ledger is a reference implementation for the atomic state transitions and a
test seam for a later adapter; it is not a production datastore.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Mapping


RESOURCE_DIMENSIONS = (
    "d1RowsRead",
    "d1RowsWritten",
    "workerRequests",
    "actionsMinutes",
    "r2Bytes",
    "r2ClassA",
    "r2ClassB",
)
METRIC_BASIS = {"measured", "estimated", "unknown"}
DECISIONS = {
    "allow",
    "warn",
    "defer_optional",
    "block_d1_bulk",
    "unknown_metrics",
    "over_budget",
    "reservation_conflict",
}
UNITS = {
    "d1RowsRead": "rows",
    "d1RowsWritten": "rows",
    "workerRequests": "requests",
    "actionsMinutes": "minutes",
    "r2Bytes": "bytes",
    "r2ClassA": "operations",
    "r2ClassB": "operations",
}


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value == 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_int(payload: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in payload:
            value = payload[key]
            if value is None or (isinstance(value, str) and value in {"unknown", "UNKNOWN"}):
                return None
            return _nonnegative_int(value, key)
    return None


def _iso_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class BudgetMetric:
    """One measured, estimated or unknown resource value."""

    value: int | None
    unit: str
    basis: str
    measured_at: str | None = None
    scope: str = ""
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.basis not in METRIC_BASIS:
            raise ValueError(f"unknown metric basis: {self.basis}")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("metric unit is required")
        if self.value is not None:
            _nonnegative_int(self.value, "metric value")
        if self.basis == "unknown" and self.value is not None:
            raise ValueError("unknown metric cannot carry a numeric value")
        if self.basis != "unknown" and self.value is None:
            raise ValueError("known metric requires a numeric value")
        if self.measured_at is not None:
            _iso_datetime(self.measured_at, "measuredAt")

    @classmethod
    def unknown(
        cls, dimension: str, *, scope: str = "", reason: str = "not_measured"
    ) -> "BudgetMetric":
        return cls(None, UNITS.get(dimension, "units"), "unknown", scope=scope, reason=reason)

    @classmethod
    def known(
        cls,
        dimension: str,
        value: int,
        *,
        basis: str,
        measured_at: str | None = None,
        scope: str = "",
        reason: str | None = None,
    ) -> "BudgetMetric":
        return cls(
            _nonnegative_int(value, dimension),
            UNITS.get(dimension, "units"),
            basis,
            measured_at=measured_at,
            scope=scope,
            reason=reason,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
        }
        if self.measured_at is not None:
            result["measuredAt"] = self.measured_at
        if self.scope:
            result["scope"] = self.scope
        if self.reason:
            result["reason"] = self.reason
        return result

    def is_fresh(self, *, now: datetime, max_age_seconds: int | None) -> bool:
        if self.basis != "measured":
            return self.basis != "unknown"
        if max_age_seconds is None:
            return True
        if self.measured_at is None:
            return False
        captured = _iso_datetime(self.measured_at, "measuredAt")
        age = now.astimezone(timezone.utc) - captured
        return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)


@dataclass(frozen=True)
class BudgetVector:
    """A complete vector over the seven quota dimensions."""

    metrics: Mapping[str, BudgetMetric]

    def __post_init__(self) -> None:
        unknown = set(self.metrics) - set(RESOURCE_DIMENSIONS)
        if unknown:
            raise ValueError(f"unknown resource dimensions: {','.join(sorted(unknown))}")
        missing = set(RESOURCE_DIMENSIONS) - set(self.metrics)
        if missing:
            raise ValueError(f"missing resource dimensions: {','.join(sorted(missing))}")

    @classmethod
    def unknown(cls, *, scope: str = "", reason: str = "not_measured") -> "BudgetVector":
        return cls({dim: BudgetMetric.unknown(dim, scope=scope, reason=reason) for dim in RESOURCE_DIMENSIONS})

    @classmethod
    def measured(
        cls,
        values: Mapping[str, Any],
        *,
        measured_at: str,
        scope: str = "account",
    ) -> "BudgetVector":
        return cls.from_mapping(values, default_basis="measured", measured_at=measured_at, scope=scope)

    @classmethod
    def estimated(cls, values: Mapping[str, Any], *, scope: str = "task") -> "BudgetVector":
        return cls.from_mapping(values, default_basis="estimated", scope=scope)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        default_basis: str | None = None,
        measured_at: str | None = None,
        scope: str = "",
    ) -> "BudgetVector":
        payload = values or {}
        metrics: dict[str, BudgetMetric] = {}
        for dimension in RESOURCE_DIMENSIONS:
            raw = payload.get(dimension)
            if raw is None:
                metrics[dimension] = BudgetMetric.unknown(
                    dimension, scope=scope, reason=f"{dimension}_missing"
                )
                continue
            if isinstance(raw, Mapping):
                basis = raw.get("basis", default_basis or "unknown")
                value = raw.get("value")
                unit = raw.get("unit", UNITS[dimension])
                metrics[dimension] = BudgetMetric(
                    None if value is None else _nonnegative_int(value, dimension),
                    str(unit),
                    str(basis),
                    measured_at=raw.get("measuredAt", measured_at),
                    scope=str(raw.get("scope", scope)),
                    reason=raw.get("reason"),
                )
                continue
            if default_basis is None:
                metrics[dimension] = BudgetMetric.unknown(
                    dimension, scope=scope, reason=f"{dimension}_basis_missing"
                )
            elif isinstance(raw, str) and raw in {"unknown", "UNKNOWN"}:
                metrics[dimension] = BudgetMetric.unknown(
                    dimension, scope=scope, reason=f"{dimension}_unknown"
                )
            else:
                metrics[dimension] = BudgetMetric.known(
                    dimension,
                    _nonnegative_int(raw, dimension),
                    basis=default_basis,
                    measured_at=measured_at,
                    scope=scope,
                )
        return cls(metrics)

    @property
    def unknown_dimensions(self) -> tuple[str, ...]:
        return tuple(dim for dim in RESOURCE_DIMENSIONS if self.metrics[dim].basis == "unknown")

    def all_known(self) -> bool:
        return not self.unknown_dimensions

    def all_fresh(self, *, now: datetime, max_age_seconds: int | None) -> bool:
        return all(metric.is_fresh(now=now, max_age_seconds=max_age_seconds) for metric in self.metrics.values())

    def add(self, other: "BudgetVector", *, scope: str = "sum") -> "BudgetVector":
        result: dict[str, BudgetMetric] = {}
        for dimension in RESOURCE_DIMENSIONS:
            left = self.metrics[dimension]
            right = other.metrics[dimension]
            if left.value is None or right.value is None:
                result[dimension] = BudgetMetric.unknown(
                    dimension, scope=scope, reason=f"{dimension}_sum_unknown"
                )
                continue
            basis = "measured" if left.basis == right.basis == "measured" else "estimated"
            result[dimension] = BudgetMetric.known(
                dimension, left.value + right.value, basis=basis, scope=scope
            )
        return BudgetVector(result)

    def as_dict(self) -> dict[str, Any]:
        return {dimension: self.metrics[dimension].as_dict() for dimension in RESOURCE_DIMENSIONS}

    def numeric(self) -> dict[str, int]:
        if not self.all_known():
            raise ValueError("resource vector contains unknown metrics")
        return {dimension: self.metrics[dimension].value for dimension in RESOURCE_DIMENSIONS}  # type: ignore[dict-item]


@dataclass(frozen=True)
class ResourceLimits:
    """Provider, target and internal hard limits."""

    d1_rows_read_per_day: int
    d1_rows_written_per_day: int
    d1_storage_bytes: int
    target_rows_read_per_day: int
    target_rows_written_per_day: int
    hard_rows_read_per_day: int
    hard_rows_written_per_day: int
    hard_storage_bytes: int
    workers_requests_per_day: int | None = None
    actions_minutes_per_month: int | None = None
    r2_bytes: int | None = None
    r2_class_a: int | None = None
    r2_class_b: int | None = None
    target_workers_requests_per_day: int | None = None
    target_actions_minutes_per_month: int | None = None
    hard_workers_requests_per_day: int | None = None
    hard_actions_minutes_per_month: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ResourceLimits":
        limits = payload.get("limits", payload)
        targets = payload.get("targets", {})
        hard = payload.get("hardLimits", {})
        storage = payload.get("storage", {})
        return cls(
            d1_rows_read_per_day=_positive_int(limits.get("d1RowsReadPerDay"), "d1RowsReadPerDay"),
            d1_rows_written_per_day=_positive_int(limits.get("d1RowsWrittenPerDay"), "d1RowsWrittenPerDay"),
            d1_storage_bytes=_positive_int(storage.get("d1Bytes"), "d1Bytes"),
            target_rows_read_per_day=_positive_int(targets.get("d1RowsReadPerDay"), "d1RowsReadPerDay"),
            target_rows_written_per_day=_positive_int(targets.get("d1RowsWrittenPerDay"), "d1RowsWrittenPerDay"),
            hard_rows_read_per_day=_positive_int(hard.get("d1RowsReadPerDay"), "d1RowsReadPerDay"),
            hard_rows_written_per_day=_positive_int(hard.get("d1RowsWrittenPerDay"), "hardD1RowsWrittenPerDay"),
            hard_storage_bytes=_positive_int(storage.get("hardD1Bytes"), "hardD1Bytes"),
            workers_requests_per_day=_optional_int(limits, "workersRequestsPerDay", "workerRequestsPerDay"),
            actions_minutes_per_month=_optional_int(limits, "githubActionsMinutesPerMonth"),
            r2_bytes=_optional_int(storage, "r2SoftBytes"),
            r2_class_a=_optional_int(limits, "r2ClassA"),
            r2_class_b=_optional_int(limits, "r2ClassB"),
            target_workers_requests_per_day=_optional_int(targets, "workersRequestsPerDay", "workerRequestsPerDay"),
            target_actions_minutes_per_month=_optional_int(targets, "githubActionsMinutesPerMonth"),
            hard_workers_requests_per_day=_optional_int(hard, "workersRequestsPerDay", "workerRequestsPerDay"),
            hard_actions_minutes_per_month=_optional_int(hard, "githubActionsMinutesPerMonth"),
        )

    def conservative_cap(self, dimension: str) -> int | None:
        official: dict[str, int | None] = {
            "d1RowsRead": self.d1_rows_read_per_day,
            "d1RowsWritten": self.d1_rows_written_per_day,
            "workerRequests": self.workers_requests_per_day,
            "actionsMinutes": self.actions_minutes_per_month,
            "r2Bytes": self.r2_bytes,
            "r2ClassA": self.r2_class_a,
            "r2ClassB": self.r2_class_b,
        }
        hard: dict[str, int | None] = {
            "d1RowsRead": self.hard_rows_read_per_day,
            "d1RowsWritten": self.hard_rows_written_per_day,
            "workerRequests": self.hard_workers_requests_per_day,
            "actionsMinutes": self.hard_actions_minutes_per_month,
            "r2Bytes": self.r2_bytes,
            "r2ClassA": self.r2_class_a,
            "r2ClassB": self.r2_class_b,
        }
        if dimension not in official:
            raise ValueError(f"unknown resource dimension: {dimension}")
        values = [value for value in (official[dimension], hard[dimension]) if value is not None]
        return min(values) if values else None

    def target_cap(self, dimension: str) -> int | None:
        values: dict[str, int | None] = {
            "d1RowsRead": self.target_rows_read_per_day,
            "d1RowsWritten": self.target_rows_written_per_day,
            "workerRequests": self.target_workers_requests_per_day,
            "actionsMinutes": self.target_actions_minutes_per_month,
        }
        return values.get(dimension)


@dataclass(frozen=True)
class ResourceUsage:
    """Legacy D1 adapter with unknown-aware parsing."""

    rows_read: int | None = None
    rows_written: int | None = None
    storage_bytes: int | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ResourceUsage":
        if not payload:
            return cls()
        return cls(
            rows_read=_optional_int(payload, "rows_read", "rowsRead"),
            rows_written=_optional_int(payload, "rows_written", "rowsWritten"),
            storage_bytes=_optional_int(payload, "storage_bytes", "storageBytes"),
        )

    def plus(self, other: "ResourceUsage") -> "ResourceUsage":
        def add(left: int | None, right: int | None) -> int | None:
            return None if left is None or right is None else left + right

        return ResourceUsage(
            rows_read=add(self.rows_read, other.rows_read),
            rows_written=add(self.rows_written, other.rows_written),
            storage_bytes=None if self.storage_bytes is None or other.storage_bytes is None else max(self.storage_bytes, other.storage_bytes),
        )


@dataclass(frozen=True)
class ResourceEstimate:
    """A legacy D1 estimate; full tasks use :class:`BudgetVector`."""

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
    estimated_read = logical_rows if rows_read is None else _nonnegative_int(rows_read, "rows_read")
    estimated_write = logical_rows * (1 + index_count) if rows_written is None else _nonnegative_int(rows_written, "rows_written")
    return ResourceEstimate(estimated_read, estimated_write, storage_bytes)


def decide(
    measured: ResourceUsage,
    estimate: ResourceEstimate,
    limits: ResourceLimits,
    *,
    optional: bool = True,
) -> dict[str, Any]:
    """Compatibility decision for D1-only stage reports."""

    unknown = [
        name
        for name, value in (
            ("rowsRead", measured.rows_read),
            ("rowsWritten", measured.rows_written),
            ("storageBytes", measured.storage_bytes),
        )
        if value is None
    ]
    projected: dict[str, int | str] = {
        "rowsRead": "unknown" if measured.rows_read is None else measured.rows_read + estimate.rows_read,
        "rowsWritten": "unknown" if measured.rows_written is None else measured.rows_written + estimate.rows_written,
        "storageBytes": "unknown" if measured.storage_bytes is None else max(measured.storage_bytes, estimate.storage_bytes),
    }
    limits_payload = {
        "officialRowsRead": limits.d1_rows_read_per_day,
        "officialRowsWritten": limits.d1_rows_written_per_day,
        "officialStorageBytes": limits.d1_storage_bytes,
        "targetRowsRead": limits.target_rows_read_per_day,
        "targetRowsWritten": limits.target_rows_written_per_day,
        "hardRowsRead": limits.hard_rows_read_per_day,
        "hardRowsWritten": limits.hard_rows_written_per_day,
        "hardStorageBytes": limits.hard_storage_bytes,
    }
    over_official = over_hard = over_target = False
    if not unknown:
        over_official = (
            projected["rowsRead"] > limits.d1_rows_read_per_day
            or projected["rowsWritten"] > limits.d1_rows_written_per_day
            or projected["storageBytes"] > limits.d1_storage_bytes
        )
        over_hard = (
            projected["rowsRead"] > limits.hard_rows_read_per_day
            or projected["rowsWritten"] > limits.hard_rows_written_per_day
            or projected["storageBytes"] > limits.hard_storage_bytes
        )
        over_target = (
            projected["rowsRead"] > limits.target_rows_read_per_day
            or projected["rowsWritten"] > limits.target_rows_written_per_day
        )
    if unknown:
        decision = "unknown_metrics"
        reason = "D1 measurement is incomplete; unknown is not treated as zero"
    elif over_official:
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
        "unknownMetrics": unknown,
        "measured": {"rowsRead": measured.rows_read, "rowsWritten": measured.rows_written, "storageBytes": measured.storage_bytes},
        "estimate": {"rowsRead": estimate.rows_read, "rowsWritten": estimate.rows_written, "storageBytes": estimate.storage_bytes},
        "projected": projected,
        "limits": limits_payload,
    }


def evaluate_budget(
    *,
    limits: ResourceLimits,
    measured: BudgetVector,
    reserved: BudgetVector,
    task_cost: BudgetVector,
    now: datetime | None = None,
    measurement_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Evaluate a complete task upper bound before any remote side effect."""

    now = now or datetime.now(timezone.utc)
    unknown: list[str] = []
    for dimension, metric in measured.metrics.items():
        if not metric.is_fresh(now=now, max_age_seconds=measurement_ttl_seconds):
            unknown.append(f"account.{dimension}")
    for prefix, vector in (("account", measured), ("reserved", reserved), ("task", task_cost)):
        unknown.extend(f"{prefix}.{dimension}" for dimension in vector.unknown_dimensions)
    caps = {dimension: limits.conservative_cap(dimension) for dimension in RESOURCE_DIMENSIONS}
    unknown.extend(f"limit.{dimension}" for dimension, value in caps.items() if value is None)
    projected: dict[str, int] = {}
    available: dict[str, int] = {}
    over: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    if not unknown:
        for dimension in RESOURCE_DIMENSIONS:
            current = measured.metrics[dimension].value
            active = reserved.metrics[dimension].value
            cost = task_cost.metrics[dimension].value
            cap = caps[dimension]
            assert current is not None and active is not None and cost is not None and cap is not None
            projected[dimension] = current + active + cost
            available[dimension] = cap - current - active
            if projected[dimension] > cap:
                over[dimension] = {"projected": projected[dimension], "limit": cap}
            target = limits.target_cap(dimension)
            if target is not None and projected[dimension] > target:
                warnings.append(dimension)
    if unknown:
        decision = "unknown_metrics"
        reason = "account, reservation, task cost or limit evidence is incomplete"
    elif over:
        decision = "over_budget"
        reason = "task upper bound would exceed the conservative available budget"
    elif warnings:
        decision = "warn"
        reason = "task fits the conservative cap but exceeds the normal target envelope"
    else:
        decision = "allow"
        reason = "task upper bound fits the conservative available budget"
    return {
        "schemaVersion": 2,
        "decision": decision,
        "reason": reason,
        "unknownMetrics": sorted(set(unknown)),
        "warnings": sorted(set(warnings)),
        "overBudget": over,
        "measured": measured.as_dict(),
        "reserved": reserved.as_dict(),
        "taskCostUpperBound": task_cost.as_dict(),
        "projected": projected,
        "available": available,
        "limits": caps,
    }


@dataclass
class _Reservation:
    task_id: str
    cost: BudgetVector
    created_at: datetime
    expires_at: datetime
    status: str = "RESERVED"
    consumed: BudgetVector | None = None
    settled: dict[str, Any] | None = None


class BudgetLedger:
    """Thread-safe reference ledger for atomic reserve/debit/settle."""

    def __init__(
        self,
        *,
        limits: ResourceLimits,
        measured: BudgetVector,
        measurement_ttl_seconds: int | None = None,
        reservation_ttl_seconds: int = 1800,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if reservation_ttl_seconds <= 0:
            raise ValueError("reservation_ttl_seconds must be positive")
        self.limits = limits
        self.measured = measured
        self.measurement_ttl_seconds = measurement_ttl_seconds
        self.reservation_ttl_seconds = reservation_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._reservations: dict[str, _Reservation] = {}
        self._settled_usage_delta = BudgetVector.measured(
            {dimension: 0 for dimension in RESOURCE_DIMENSIONS},
            measured_at=self._now().isoformat().replace("+00:00", "Z"),
            scope="settled_usage",
        )
        self._version = 0

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _active_reserved(self) -> BudgetVector:
        total = BudgetVector.estimated({dimension: 0 for dimension in RESOURCE_DIMENSIONS}, scope="active_reservations")
        for reservation in self._reservations.values():
            if reservation.status in {"RESERVED", "RUNNING", "UNKNOWN", "OVER_BUDGET"}:
                total = total.add(reservation.cost, scope="active_reservations")
        return total

    def _current_usage(self) -> BudgetVector:
        return self.measured.add(self._settled_usage_delta, scope="account_plus_settled")

    def reserve(self, task_id: str, task_cost: BudgetVector) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required")
        if not task_cost.all_known():
            return {"decision": "unknown_metrics", "taskId": task_id, "unknownMetrics": list(task_cost.unknown_dimensions)}
        with self._lock:
            now = self._now()
            self.expire(now=now)
            existing = self._reservations.get(task_id)
            if existing is not None:
                if existing.cost.as_dict() != task_cost.as_dict():
                    raise ValueError("budget_reservation_conflict")
                return self._reservation_result(existing, decision="idempotent")
            decision = evaluate_budget(
                limits=self.limits,
                measured=self._current_usage(),
                reserved=self._active_reserved(),
                task_cost=task_cost,
                now=now,
                measurement_ttl_seconds=self.measurement_ttl_seconds,
            )
            if decision["decision"] not in {"allow", "warn"}:
                decision["taskId"] = task_id
                return decision
            reservation = _Reservation(
                task_id=task_id,
                cost=task_cost,
                created_at=now,
                expires_at=now + timedelta(seconds=self.reservation_ttl_seconds),
            )
            self._reservations[task_id] = reservation
            self._version += 1
            return self._reservation_result(reservation, decision=decision["decision"])

    def mark_running(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            reservation = self._get(task_id)
            if reservation.status == "RESERVED":
                reservation.status = "RUNNING"
                self._version += 1
            elif reservation.status not in {"RUNNING"}:
                raise ValueError(f"reservation_not_running:{reservation.status}")
            return self._reservation_result(reservation, decision="running")

    def debit(self, task_id: str, delta: BudgetVector) -> dict[str, Any]:
        """Atomically record physical counters observed during execution."""

        if not delta.all_known():
            raise ValueError("debit_requires_known_metrics")
        with self._lock:
            reservation = self._get(task_id)
            if reservation.status not in {"RESERVED", "RUNNING"}:
                return self._reservation_result(reservation, decision="not_debitable")
            previous = reservation.consumed or BudgetVector.measured(
                {dimension: 0 for dimension in RESOURCE_DIMENSIONS},
                measured_at=self._now().isoformat().replace("+00:00", "Z"),
                scope="execution_delta",
            )
            consumed = previous.add(delta, scope="execution_consumed")
            reservation.consumed = consumed
            exceeds = {
                dimension: {"consumed": consumed.metrics[dimension].value, "reserved": reservation.cost.metrics[dimension].value}
                for dimension in RESOURCE_DIMENSIONS
                if consumed.metrics[dimension].value is not None
                and reservation.cost.metrics[dimension].value is not None
                and consumed.metrics[dimension].value > reservation.cost.metrics[dimension].value
            }
            if exceeds:
                reservation.status = "OVER_BUDGET"
                self._version += 1
                return {**self._reservation_result(reservation, decision="over_budget"), "overBudget": exceeds}
            reservation.status = "RUNNING"
            self._version += 1
            return self._reservation_result(reservation, decision="debited")

    def settle(
        self,
        task_id: str,
        *,
        outcome: str,
        actual: BudgetVector | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Settle a known result or retain the reservation for UNKNOWN."""

        if outcome not in {"SUCCEEDED", "FAILED_KNOWN", "UNKNOWN", "TIMEOUT"}:
            raise ValueError("invalid_budget_outcome")
        with self._lock:
            reservation = self._get(task_id)
            # UNKNOWN/TIMEOUT is a provisional terminal result: retain the
            # reservation, but allow a later measured receipt to reconcile it.
            # Repeating an unknown result remains idempotent and cannot clear
            # the conservative hold.
            if reservation.settled is not None and reservation.status != "UNKNOWN":
                return reservation.settled
            if outcome in {"UNKNOWN", "TIMEOUT"}:
                if reservation.settled is not None:
                    return reservation.settled
                reservation.status = "UNKNOWN"
                result = self._reservation_result(reservation, decision="unknown_outcome")
                result["reason"] = reason or "result_not_confirmed; reservation_retained"
                reservation.settled = result
                self._version += 1
                return result
            if actual is None or not actual.all_known():
                raise ValueError("known_settlement_requires_complete_metrics")
            if any(metric.basis != "measured" for metric in actual.metrics.values()):
                raise ValueError("known_settlement_requires_measured_metrics")
            over = {
                dimension: {"actual": actual.metrics[dimension].value, "reserved": reservation.cost.metrics[dimension].value}
                for dimension in RESOURCE_DIMENSIONS
                if actual.metrics[dimension].value is not None
                and reservation.cost.metrics[dimension].value is not None
                and actual.metrics[dimension].value > reservation.cost.metrics[dimension].value
            }
            if over:
                reservation.status = "OVER_BUDGET"
                result = {**self._reservation_result(reservation, decision="over_budget"), "overBudget": over}
                reservation.settled = result
                self._version += 1
                return result
            reservation.status = outcome
            self._settled_usage_delta = self._settled_usage_delta.add(actual, scope="settled_usage")
            result = self._reservation_result(reservation, decision="settled")
            result["actual"] = actual.as_dict()
            result["reason"] = reason
            reservation.settled = result
            self._version += 1
            return result

    def expire(self, *, now: datetime | None = None) -> tuple[str, ...]:
        with self._lock:
            instant = (now or self._now()).astimezone(timezone.utc)
            expired: list[str] = []
            for task_id, reservation in self._reservations.items():
                if reservation.status in {"RESERVED", "RUNNING"} and instant >= reservation.expires_at:
                    reservation.status = "UNKNOWN"
                    expired.append(task_id)
            if expired:
                self._version += 1
            return tuple(sorted(expired))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = self._active_reserved()
            return {
                "version": self._version,
                "measured": self._current_usage().as_dict(),
                "reserved": active.as_dict(),
                "reservations": {
                    task_id: self._reservation_result(reservation, decision="snapshot")
                    for task_id, reservation in sorted(self._reservations.items())
                },
            }

    def _get(self, task_id: str) -> _Reservation:
        try:
            return self._reservations[task_id]
        except KeyError as exc:
            raise ValueError("budget_reservation_not_found") from exc

    @staticmethod
    def _reservation_result(reservation: _Reservation, *, decision: str) -> dict[str, Any]:
        return {
            "decision": decision,
            "taskId": reservation.task_id,
            "status": reservation.status,
            "costUpperBound": reservation.cost.as_dict(),
            "createdAt": reservation.created_at.isoformat().replace("+00:00", "Z"),
            "expiresAt": reservation.expires_at.isoformat().replace("+00:00", "Z"),
            "consumed": reservation.consumed.as_dict() if reservation.consumed else None,
        }


__all__ = [
    "BudgetLedger",
    "BudgetMetric",
    "BudgetVector",
    "DECISIONS",
    "METRIC_BASIS",
    "RESOURCE_DIMENSIONS",
    "ResourceEstimate",
    "ResourceLimits",
    "ResourceUsage",
    "decide",
    "estimate_stage",
    "evaluate_budget",
]
