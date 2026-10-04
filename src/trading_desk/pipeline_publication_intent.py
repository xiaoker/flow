"""Bounded local publication intent, committed before candidate HTTP effects.

Not a remote coordinator or cross-host receipt service. Every process must use
the same retained SQLite file. Missing/corrupt state fails closed; no reset,
prune or permission API. Reuses producer canonical bytes for local digests.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .read_models import canonical_json_bytes

MAX_INTENTS = 40
MAX_RECORD_BYTES = 16_384


def _encoded(value):
    result = canonical_json_bytes(value)
    if len(result) > MAX_RECORD_BYTES:
        raise ValueError("publication_intent_record_limit")
    return result.decode()


class PublicationIntentStore:
    @classmethod
    def initialize(cls, path: Path, *, execution: dict, release_id: str, manifest_sha256: str):
        path = Path(path).absolute()
        cls._safe(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return cls(path, execution=execution, release_id=release_id,
                   manifest_sha256=manifest_sha256, _initialize=True)

    @staticmethod
    def _safe(path):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("publication_intent_symlink")

    def __init__(self, path: Path, *, execution: dict, release_id: str,
                 manifest_sha256: str, _initialize=False):
        self.path = Path(path).absolute()
        self._safe(self.path)
        if not self.path.is_file():
            raise ValueError("publication_intent_missing")
        # Execution authority is checked by the caller's existing guard/server.
        # This contract only prevents local state from being reused for another
        # run/owner/attempt/date/build or immutable publication.
        self._contract = _encoded({"schemaVersion": 1, "execution": execution,
                                   "releaseId": release_id, "manifestSha256": manifest_sha256})
        with self._connect() as db:
            if _initialize:
                db.execute("CREATE TABLE contract (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
                db.execute("INSERT INTO contract VALUES (1,?)", (self._contract,))
                db.execute("CREATE TABLE intents (name TEXT NOT NULL, finalize INTEGER NOT NULL CHECK(finalize IN (0,1)), reference TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','ACKNOWLEDGED')), receipt TEXT, digest TEXT NOT NULL, PRIMARY KEY(name,finalize))")
            self._check(db)

    @contextmanager
    def _connect(self):
        self._safe(self.path)
        if not self.path.is_file():
            raise ValueError("publication_intent_missing")
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _check(self, db):
        row = db.execute("SELECT payload FROM contract WHERE id=1").fetchone()
        if row != (self._contract,):
            raise ValueError("publication_intent_identity_mismatch")

    def _reference(self, reference):
        if not isinstance(reference, dict) or set(reference) != {"execution", "releaseId", "manifestSha256", "name", "finalize", "artifactSha256", "artifactBytes", "artifactKey"}:
            raise ValueError("publication_intent_reference_invalid")
        bound = json.loads(self._contract)
        if any(reference.get(key) != bound[key] for key in ("execution", "releaseId", "manifestSha256")):
            raise ValueError("publication_intent_identity_mismatch")
        if (not isinstance(reference["name"], str) or not 1 <= len(reference["name"]) <= 64
                or type(reference["finalize"]) is not bool):
            raise ValueError("publication_intent_reference_invalid")
        return _encoded(reference)

    @staticmethod
    def _digest(reference, status, receipt):
        return hashlib.sha256(canonical_json_bytes({"reference": reference, "status": status, "receipt": receipt})).hexdigest()

    def _rows(self, db):
        self._check(db)
        rows = db.execute("SELECT name,finalize,reference,status,receipt,digest FROM intents ORDER BY name,finalize LIMIT ?", (MAX_INTENTS + 1,)).fetchall()
        if len(rows) > MAX_INTENTS:
            raise ValueError("publication_intent_capacity")
        records = {}
        for name, final, reference, status, receipt, digest in rows:
            if (len(reference.encode()) > MAX_RECORD_BYTES or (receipt is not None and len(receipt.encode()) > MAX_RECORD_BYTES)
                    or self._digest(reference, status, receipt) != digest):
                raise ValueError("publication_intent_corrupt")
            value = json.loads(reference)
            if self._reference(value) != reference or (name, bool(final)) != (value["name"], value["finalize"]):
                raise ValueError("publication_intent_corrupt")
            if status not in {"PENDING", "ACKNOWLEDGED"} or (status == "PENDING" and receipt is not None) or (status == "ACKNOWLEDGED" and receipt is None):
                raise ValueError("publication_intent_corrupt")
            records[(name, bool(final))] = {"reference": value, "status": status,
                "receipt": json.loads(receipt) if receipt is not None else None}
        return records

    def lookup(self, reference):
        encoded = self._reference(reference)
        with self._connect() as db:
            records = self._rows(db)
            record = records.get((reference["name"], reference["finalize"]))
            if record is not None and _encoded(record["reference"]) != encoded:
                raise ValueError("publication_intent_reference_conflict")
            return record

    def has_pending(self):
        with self._connect() as db:
            return any(row["status"] == "PENDING" for row in self._rows(db).values())

    def begin(self, reference):
        encoded = self._reference(reference)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = self._rows(db)
            if any(row["status"] == "PENDING" for row in records.values()):
                raise ValueError("publication_intent_pending_requires_reconciliation")
            if (reference["name"], reference["finalize"]) in records:
                raise ValueError("publication_intent_already_recorded")
            if len(records) >= MAX_INTENTS:
                raise ValueError("publication_intent_capacity")
            db.execute("INSERT INTO intents VALUES (?,?,?,'PENDING',NULL,?)", (reference["name"], int(reference["finalize"]), encoded,
                self._digest(encoded, "PENDING", None)))
            db.commit()  # No HTTP permission returns before this durable commit.

    def acknowledge(self, reference, receipt):
        encoded = self._reference(reference)
        receipt_json = _encoded(receipt)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = self._rows(db)
            record = records.get((reference["name"], reference["finalize"]))
            if record is None or record["reference"] != reference:
                raise ValueError("publication_intent_reference_conflict")
            if record["status"] == "ACKNOWLEDGED":
                if record["receipt"] != receipt:
                    raise ValueError("publication_intent_receipt_conflict")
                return
            db.execute("UPDATE intents SET status='ACKNOWLEDGED',receipt=?,digest=? WHERE name=? AND finalize=? AND status='PENDING'", (
                receipt_json, self._digest(encoded, "ACKNOWLEDGED", receipt_json), reference["name"], int(reference["finalize"])))
            db.commit()
