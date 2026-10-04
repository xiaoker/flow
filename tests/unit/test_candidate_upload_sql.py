"""Execute the actual candidate staging SQL against SQLite, not a SQL mock."""
from pathlib import Path
import re
import sqlite3

import pytest


SOURCE = Path(__file__).parents[2] / "web/app/lib/candidate-upload-authority.ts"
SQL = re.search(r"db\.prepare\(`(INSERT INTO release_manifest.*?)`\)", SOURCE.read_text(), re.S).group(1)


def database():
    db = sqlite3.connect(":memory:")
    db.executescript("""
      CREATE TABLE pipeline_window (run_key TEXT PRIMARY KEY, lease_owner TEXT,
        attempt_count INTEGER, target_date TEXT, recovery_id TEXT, status TEXT, lease_expires_at TEXT);
      CREATE TABLE release_manifest (release_id TEXT PRIMARY KEY, as_of TEXT, schema_version INTEGER,
        manifest_sha256 TEXT, manifest_json TEXT, observed_at TEXT, status TEXT, created_at TEXT);
      INSERT INTO pipeline_window VALUES ('run', 'owner', 2, '2026-09-29', NULL, 'RUNNING', '2026-10-04T00:15:00Z');
    """)
    return db


PARAMS = ("release", "2026-09-29", 4, "hash", "{}", None, "2026-10-04T00:00:00Z",
          "run", "owner", 2, "2026-09-29", "2026-10-04T00:00:30Z")


@pytest.mark.parametrize("column,value", [
    ("lease_owner", "new-owner"), ("attempt_count", 3), ("target_date", "2026-09-30"),
    ("status", "COMPLETED"), ("recovery_id", "recovery"),
    ("lease_expires_at", "2026-10-04T00:00:30Z"), ("lease_expires_at", "invalid"),
])
def test_conditional_insert_rejects_stale_authority(column, value):
    db = database()
    db.execute(f"UPDATE pipeline_window SET {column} = ? WHERE run_key = 'run'", (value,))
    assert db.execute(SQL, PARAMS).rowcount == 0
    assert db.execute("SELECT count(*) FROM release_manifest").fetchone()[0] == 0


def test_insert_is_idempotent_and_never_overwrites_existing_active_release():
    db = database()
    assert db.execute(SQL, PARAMS).rowcount == 1
    assert db.execute(SQL, PARAMS).rowcount == 0
    db.execute("UPDATE release_manifest SET status = 'ACTIVE', manifest_json = 'original'")
    assert db.execute(SQL, PARAMS).rowcount == 0
    assert db.execute("SELECT status, manifest_json FROM release_manifest").fetchone() == ("ACTIVE", "original")


def test_authority_predicate_uses_primary_key_lookup():
    plan = [row[3] for row in database().execute("EXPLAIN QUERY PLAN " + SQL, PARAMS)]
    assert any("SEARCH pipeline_window" in item and "USING INDEX" in item and "run_key=?" in item for item in plan)
    assert not any("SCAN pipeline_window" in item for item in plan)
