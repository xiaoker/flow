"""Bounded local durable adapter for the existing budget state machine.

This is NOT a distributed/account-wide quota service. Processes must share the
same local database and unchanged evidence epoch. No remote I/O or production
caller is enabled. SQLite transactions serialize admission and commit allowance
before the caller can perform I/O; ambiguous outcomes never clear the journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .resource_budget import BudgetLedger, BudgetVector, ResourceLimits
from . import resource_budget

MAX_EVENTS = 2048
MAX_EVENT_BYTES = 16_384
MAX_JOURNAL_BYTES = 2_097_152


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class LocalBudgetLedger(BudgetLedger):
    """Same ledger transitions, replayed within one local SQLite transaction.

    Initialization is explicit and exclusive. Reopening missing/corrupt state
    fails closed, never silently replaces spent allowance with an empty ledger.
    There is no pruning/reset API. Limits/evidence/TTLs cannot change on reopen.
    """

    @classmethod
    def initialize(cls, path: Path, **kwargs: Any) -> "LocalBudgetLedger":
        path = Path(path)
        # Exclusive creation prevents two initializers from resetting one store.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        ledger = cls(path, _initialize=True, **kwargs)
        return ledger

    def __init__(
        self, path: Path, *, limits: ResourceLimits, measured: BudgetVector,
        measurement_ttl_seconds: int | None = None,
        reservation_ttl_seconds: int = 1800,
        clock: Callable[[], datetime] | None = None,
        _initialize: bool = False,
    ) -> None:
        self.path = Path(path).absolute()
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("local_budget_store_missing_or_symlink")
        self.limits = limits
        self.measured = measured
        self.measurement_ttl_seconds = measurement_ttl_seconds
        self.reservation_ttl_seconds = reservation_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Validate the existing constructor contract rather than duplicate it.
        self._new_ledger(self._instant())
        self._contract = self._configuration_contract()
        self._contract_hash = hashlib.sha256(self._contract.encode()).hexdigest()
        with self._connect() as db:
            if _initialize:
                db.execute("CREATE TABLE contract (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
                db.execute("INSERT INTO contract VALUES (1, ?)", (self._contract,))
                db.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL)")
                db.execute("CREATE TABLE head (id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER NOT NULL, digest TEXT NOT NULL)")
                db.execute("INSERT INTO head VALUES (1,0,?)", (self._contract_hash,))
            self._check_contract(db)

    def _configuration_contract(self) -> str:
        engine_digest = hashlib.sha256(Path(resource_budget.__file__).read_bytes()).hexdigest()
        return _encode({"schemaVersion": 1, "engineDigest": engine_digest,
                       "limits": asdict(self.limits), "measured": self.measured.as_dict(),
                       "measurementTtl": self.measurement_ttl_seconds,
                       "reservationTtl": self.reservation_ttl_seconds})

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _instant(self) -> datetime:
        instant = self._clock()
        if instant.tzinfo is None:
            raise ValueError("budget_clock_requires_timezone")
        return instant.astimezone(timezone.utc)

    def _new_ledger(self, instant: datetime) -> BudgetLedger:
        return BudgetLedger(limits=self.limits, measured=self.measured,
                            measurement_ttl_seconds=self.measurement_ttl_seconds,
                            reservation_ttl_seconds=self.reservation_ttl_seconds,
                            clock=lambda: instant)

    def _check_contract(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT payload FROM contract WHERE id=1").fetchone()
        if row is None or row[0] != self._contract or self._configuration_contract() != self._contract:
            raise ValueError("local_budget_evidence_epoch_conflict")

    @staticmethod
    def _apply(ledger: BudgetLedger, event: dict[str, Any]) -> Any:
        instant = datetime.fromisoformat(event["at"])
        if instant.tzinfo is None:
            raise ValueError("local_budget_invalid_event_time")
        ledger._clock = lambda: instant
        op = event["op"]
        args = event["args"]
        if op in {"reserve", "debit", "authorize_step"}:
            return getattr(ledger, op)(args["task"], BudgetVector.from_mapping(args["vector"]))
        if op == "mark_running":
            return ledger.mark_running(args["task"])
        if op == "settle":
            actual = BudgetVector.from_mapping(args["actual"]) if args["actual"] is not None else None
            return ledger.settle(args["task"], outcome=args["outcome"], actual=actual, reason=args["reason"])
        if op == "expire":
            return ledger.expire(now=instant)
        if op == "snapshot":
            return ledger.snapshot()
        raise ValueError("local_budget_unknown_operation")

    def _execute(self, op: str, args: dict[str, Any], *, at: datetime | None = None) -> Any:
        instant = at or self._instant()
        if instant.tzinfo is None:
            raise ValueError("budget_clock_requires_timezone")
        event = {"op": op, "args": args, "at": instant.astimezone(timezone.utc).isoformat()}
        encoded = _encode(event)
        if len(encoded.encode()) > MAX_EVENT_BYTES:
            raise ValueError("local_budget_event_too_large")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_contract(db)
            # Hard bounds, no unlimited journal scan; fail closed at capacity.
            totals = db.execute("SELECT count(*), coalesce(sum(length(CAST(payload AS BLOB))),0) FROM events").fetchone()
            if totals[0] >= MAX_EVENTS or totals[1] + len(encoded.encode()) > MAX_JOURNAL_BYTES:
                raise ValueError("local_budget_journal_capacity")
            ledger = self._new_ledger(instant)
            previous = self._contract_hash
            count = 0
            last_at = None
            for sequence, payload, digest in db.execute(
                "SELECT sequence,payload,digest FROM events ORDER BY sequence LIMIT ?", (MAX_EVENTS,)
            ):
                expected = hashlib.sha256((previous + payload).encode()).hexdigest()
                count += 1
                if sequence != count or expected != digest:
                    raise ValueError("local_budget_journal_corrupt")
                decoded = json.loads(payload)
                if count == 1:
                    ledger = self._new_ledger(datetime.fromisoformat(decoded["at"]))
                self._apply(ledger, decoded)
                last_at = datetime.fromisoformat(decoded["at"])
                previous = digest
            head = db.execute("SELECT sequence,digest FROM head WHERE id=1").fetchone()
            if head != (count, previous):
                raise ValueError("local_budget_journal_corrupt")
            if last_at is not None and instant < last_at:
                raise ValueError("local_budget_clock_regression")
            result = self._apply(ledger, event)
            digest = hashlib.sha256((previous + encoded).encode()).hexdigest()
            db.execute("INSERT INTO events VALUES (?,?,?)", (totals[0] + 1, encoded, digest))
            db.execute("UPDATE head SET sequence=?,digest=? WHERE id=1", (totals[0] + 1, digest))
            # Commit precedes returning permission. Errors rollback and do not
            # return successful admission or authorize external side effects.
            db.commit()
            return result

    def reserve(self, task_id: str, task_cost: BudgetVector) -> dict[str, Any]:
        return self._execute("reserve", {"task": task_id, "vector": task_cost.as_dict()})

    def mark_running(self, task_id: str) -> dict[str, Any]:
        return self._execute("mark_running", {"task": task_id})

    def debit(self, task_id: str, delta: BudgetVector) -> dict[str, Any]:
        return self._execute("debit", {"task": task_id, "vector": delta.as_dict()})

    def authorize_step(self, task_id: str, cost_upper_bound: BudgetVector) -> dict[str, Any]:
        return self._execute("authorize_step", {"task": task_id, "vector": cost_upper_bound.as_dict()})

    def settle(self, task_id: str, *, outcome: str, actual: BudgetVector | None = None,
               reason: str | None = None) -> dict[str, Any]:
        return self._execute("settle", {"task": task_id, "outcome": outcome,
                                       "actual": actual.as_dict() if actual else None, "reason": reason})

    def expire(self, *, now: datetime | None = None) -> tuple[str, ...]:
        return self._execute("expire", {}, at=now)

    def snapshot(self) -> dict[str, Any]:
        return self._execute("snapshot", {})
