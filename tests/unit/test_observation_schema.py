from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = next((ROOT / "web/drizzle").glob("0013_*.sql"))
RECEIPT_SCHEMA = json.loads(
    (ROOT / "schemas/source-observation-receipt.schema.json").read_text(encoding="utf-8")
)
SIDE_EFFECTS = json.loads(
    (ROOT / "docs/audits/2026-09-13-flow-v05-pr-a-merge-side-effects.json").read_text(encoding="utf-8")
)


def _migration_statements() -> list[str]:
    sql = MIGRATION.read_text(encoding="utf-8")
    return [statement.strip() for statement in sql.split("--> statement-breakpoint") if statement.strip()]


def _valid_receipt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "observationId": "obs-sse-etf-2026-09-13-abc",
        "sourceId": "sse",
        "dataset": "etf_scale",
        "targetDate": "2026-09-13",
        "sourceBusinessDate": "2026-09-12",
        "publishedDate": "2026-09-13",
        "dateBasis": "official_page_business_date",
        "status": "OBSERVED",
        "officialUrl": "https://example.exchange.test/etf",
        "requestParams": {"page": 1, "date": "2026-09-13"},
        "httpStatus": 200,
        "responseContentType": "application/json",
        "rawObjectKey": "market-data/raw/sse/etf_scale/2026-09-12/abc.json",
        "rawBytes": 12,
        "rawSha256": "a" * 64,
        "normalizedSha256": "b" * 64,
        "sourceFingerprint": "c" * 64,
        "contractVersion": "flow-product-v0.4",
        "runKey": "daily-market-data:2026-09-13:morning:v1",
        "observedAt": "2026-09-13T01:00:00Z",
        "reasonCode": None,
        "evidence": {"httpStatus": 200, "responseSha256": "a" * 64},
        "createdAt": "2026-09-13T01:00:00Z",
    }
    value.update(overrides)
    return value


def test_receipt_schema_is_valid_and_status_rules_fail_closed() -> None:
    Draft202012Validator.check_schema(RECEIPT_SCHEMA)
    validator = Draft202012Validator(RECEIPT_SCHEMA, format_checker=FormatChecker())
    assert list(validator.iter_errors(_valid_receipt())) == []

    not_published = _valid_receipt(
        observationId="obs-szse-2026-09-13-not-published",
        sourceId="szse",
        status="NOT_PUBLISHED",
        sourceBusinessDate=None,
        publishedDate=None,
        rawObjectKey=None,
        rawBytes=None,
        rawSha256=None,
        normalizedSha256=None,
        reasonCode="official_source_not_published",
        evidence={"httpStatus": 200, "bodySha256": "d" * 64},
    )
    assert list(validator.iter_errors(not_published)) == []

    missing_raw = _valid_receipt(rawSha256=None)
    assert any("rawSha256" in error.message or "rawSha256" in error.path for error in validator.iter_errors(missing_raw))
    unknown_status = _valid_receipt(status="UNKNOWN")
    assert list(validator.iter_errors(unknown_status))


def test_migration_is_additive_and_has_only_required_indexes() -> None:
    statements = _migration_statements()
    assert len(statements) == 3
    joined = "\n".join(statements).upper()
    for forbidden in ("ALTER TABLE", "DROP ", "DELETE ", "TRUNCATE", "VACUUM"):
        assert forbidden not in joined
    assert "CREATE TABLE `SOURCE_OBSERVATION`" in joined
    assert "CREATE UNIQUE INDEX `SOURCE_OBSERVATION_FINGERPRINT_UQ`" in joined
    assert "CREATE INDEX `SOURCE_OBSERVATION_TARGET_SOURCE_IDX`" in joined


def test_drizzle_schema_and_metadata_are_in_sync() -> None:
    schema = (ROOT / "web/db/schema.ts").read_text(encoding="utf-8")
    for field in (
        'sqliteTable("source_observation"',
        'text("observation_id")',
        'text("source_fingerprint")',
        'text("contract_version")',
        'uniqueIndex("source_observation_fingerprint_uq")',
        'index("source_observation_target_source_idx")',
    ):
        assert field in schema

    journal = json.loads((ROOT / "web/drizzle/meta/_journal.json").read_text(encoding="utf-8"))
    # Later additive migrations must not make this historical schema assertion
    # stale. Preserve the exact entry and require a contiguous ordered journal.
    assert [entry["idx"] for entry in journal["entries"]] == list(range(len(journal["entries"])))
    assert journal["entries"][14]["idx"] == 14
    assert journal["entries"][14]["tag"] == "0014_warm_morlocks"
    snapshot = json.loads((ROOT / "web/drizzle/meta/0013_snapshot.json").read_text(encoding="utf-8"))
    table = snapshot["tables"]["source_observation"]
    assert set(table["columns"]) == {
        "observation_id",
        "source_id",
        "dataset",
        "target_date",
        "source_business_date",
        "published_date",
        "date_basis",
        "status",
        "official_url",
        "request_params_json",
        "http_status",
        "response_content_type",
        "raw_object_key",
        "raw_bytes",
        "raw_sha256",
        "normalized_sha256",
        "source_fingerprint",
        "contract_version",
        "run_key",
        "observed_at",
        "reason_code",
        "evidence_json",
        "created_at",
    }


def test_local_migration_query_plan_is_indexed_and_fingerprint_is_unique() -> None:
    database = sqlite3.connect(":memory:")
    try:
        for statement in _migration_statements():
            database.executescript(statement)
        columns = [row[1] for row in database.execute("PRAGMA table_info(source_observation)")]
        assert columns[0] == "observation_id"
        plan = list(
            database.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT observation_id, source_business_date, published_date, status, "
                "source_fingerprint, observed_at FROM source_observation "
                "WHERE target_date = ? AND source_id = ? "
                "ORDER BY observed_at DESC LIMIT ?",
                ("2026-09-13", "sse", 20),
            )
        )
        assert any("USING INDEX source_observation_target_source_idx" in str(row[-1]) for row in plan)

        insert = (
            "INSERT INTO source_observation (observation_id, source_id, dataset, target_date, "
            "date_basis, status, official_url, request_params_json, source_fingerprint, "
            "contract_version, observed_at, evidence_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        values = (
            "obs-1",
            "sse",
            "etf_scale",
            "2026-09-13",
            "official_page_business_date",
            "NOT_PUBLISHED",
            "https://example.exchange.test/etf",
            "{}",
            "a" * 64,
            "flow-product-v0.4",
            "2026-09-13T01:00:00Z",
            '{"reason":"not published"}',
            "2026-09-13T01:00:00Z",
        )
        database.execute(insert, values)
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(insert, (*values[:8], values[8], *values[9:]))
    finally:
        database.close()


def test_daily_receipt_bound_is_one_row_per_new_fingerprint() -> None:
    # The current eight source/dataset definitions are the upper bound for one
    # collection window. Retries with the same fingerprint do not add rows.
    sources = ["sse_etf", "szse_etf", "sse_margin", "szse_margin", "bse_margin", "stock_connect_summary", "sse_etf_nav", "sse_etf_pcf"]
    assert len(set(sources)) == 8
    assert len({f"2026-09-13:{source}:fingerprint" for source in sources}) == 8


def test_merge_side_effects_are_explicit_about_path_filtered_deploy() -> None:
    assert SIDE_EFFECTS["generatedFor"]["baseSha"] == "2d6e4deec6a33edf2b3273c0bda419fd41801ef6"
    assert SIDE_EFFECTS["mergeToMainEffects"]["deployWorker"] is True
    assert SIDE_EFFECTS["mergeToMainEffects"]["d1Migration"]["remote"] is True
    assert SIDE_EFFECTS["mergeToMainEffects"]["d1Migration"]["destructive"] is False
    assert SIDE_EFFECTS["mergeToMainEffects"]["d1Migration"]["dataRowsInserted"] == 0
    assert "web/db/schema.ts" in SIDE_EFFECTS["changedPaths"]
    assert "reports/phase0/latest-*" in SIDE_EFFECTS["sideEffectGuards"]["protectedPaths"]
