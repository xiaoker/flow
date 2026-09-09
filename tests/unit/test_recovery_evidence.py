from __future__ import annotations

import ast
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_desk.recovery_audit import audit_bundle
from trading_desk.recovery_execution import canonical_sha256
from trading_desk.recovery_evidence import (
    EVIDENCE_CONTRACT,
    EVIDENCE_SCHEMA_VERSION,
    MAX_PAGES,
    MAX_R2_TOTAL_HEADS,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_REQUESTS,
    PRIORITY_DATES,
    RecoveryEvidenceError,
    RecoveryEvidenceExporter,
    build_evidence_bundle,
    load_official_usage,
    to_audit_input,
    validate_evidence_bundle,
    audit_twice,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "config/resource-budget.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "schemas/recovery-evidence-bundle.schema.json").read_text(encoding="utf-8"))
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
CAPTURED = "2026-09-07T01:00:00Z"


def cursor(value: str) -> str:
    raw = json.dumps({"v": 1, "date": value}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def domain_record(data_date: str, *, dashboard: str = "UNKNOWN", penetration: str = "UNKNOWN", raw: str = "UNKNOWN") -> dict:
    return {
        "dataDate": data_date,
        "domain": "sse_etf",
        "market": "SSE",
        "applicability": "REQUIRED",
        "stageStatus": {
            "raw": raw, "normalized": "READY", "model": "READY", "pcf_exact": "READY",
            "dashboard": dashboard, "etf_penetration": penetration, "date_visible": dashboard,
        },
        "evidence": {
            "raw": [{"objectKey": f"market-data/raw/{data_date}.csv", "sha256": SHA_A, "rowCount": 10, "sourceBusinessDate": data_date, "sourceFingerprint": SHA_C}],
            "normalized": [{"objectKey": f"market-data/normalized/{data_date}.json", "sha256": SHA_B, "rowCount": 10}],
            "pcfExact": [{"objectKey": f"market-data/normalized/{data_date}.pcf.json", "sha256": SHA_C, "rowCount": 10, "pcfDate": data_date}],
            "dashboard": [], "etfPenetration": [], "dateVisible": [{"source": "historical_snapshot"}],
        },
    }


def r2_cursor(key: str) -> str:
    raw = json.dumps({"v": 1, "start": "2024-08-01", "end": "2026-09-03", "key": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def page(
    data_dates: list[str],
    *,
    request_cursor: str | None = None,
    r2_request_cursor: str | None = None,
    has_more: bool = False,
    next_cursor: str | None = None,
    r2_has_more: bool = False,
    r2_next_cursor: str | None = None,
    r2_head_count: int = 0,
    status: str = "COMPLETE",
    incomplete_reasons: list[str] | None = None,
    r2_index_sha: str = SHA_A,
) -> bytes:
    dates = []
    for data_date in data_dates:
        dates.append({"dataDate": data_date, "domains": [domain_record(data_date)], "releases": {"dashboard": [], "etf_penetration": []}, "sourceReadiness": [], "artifactPointers": [], "pipelineWindows": [], "recoveries": [], "marketHistory": None, "dateVisible": {"status": "UNKNOWN", "evidence": []}})
    body = {
        "schemaVersion": 1,
        "contract": "recovery-inventory-v1",
        "request": {"start": "2024-08-01", "end": "2026-09-03", "cursor": request_cursor, "limit": 100, "r2Cursor": r2_request_cursor},
        "status": status,
        "hasMore": has_more,
        "nextCursor": next_cursor,
        "activeReleaseSlots": [],
        "dates": dates,
        "usage": {"d1": {"queryCount": 1, "rowsRead": 10, "rowsWritten": 0, "rowsReadKnown": True, "rowsWrittenKnown": True}, "r2": {"classA": 0, "classB": r2_head_count, "headCount": r2_head_count, "headLimit": 64}, "truncated": False},
        "incompleteReasons": incomplete_reasons or [],
        "r2Pagination": {
            "cursor": r2_request_cursor,
            "nextCursor": r2_next_cursor,
            "hasMore": r2_has_more,
            "pageSize": r2_head_count if r2_has_more or r2_request_cursor else 0,
            "candidateCount": r2_head_count if r2_has_more or r2_request_cursor else 0,
            "indexSha256": r2_index_sha,
        },
    }
    return json.dumps(body, separators=(",", ":")).encode()


class FixtureTransport:
    def __init__(self, pages: dict[str | None, tuple[int, bytes] | Exception]):
        self.pages = pages
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.requests.append((url, dict(headers)))
        query = url.split("?", 1)[1]
        params = dict(item.split("=", 1) for item in query.split("&"))
        token = params.get("cursor")
        if token:
            token = urllib_unquote(token)
        r2_token = params.get("r2Cursor")
        if r2_token:
            r2_token = urllib_unquote(r2_token)
        result = self.pages.get((token, r2_token), self.pages.get(token))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError(f"unexpected cursor {token!r}")
        return result


def urllib_unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def exporter(transport: FixtureTransport) -> RecoveryEvidenceExporter:
    return RecoveryEvidenceExporter(
        base_url="https://flow.test",
        ingest_api_key="fixture-ingest",
        access_client_id="fixture-client",
        access_client_secret="fixture-client-credential",
        transport=transport,
        captured_at=CAPTURED,
    )


def test_fixture_replay_is_bounded_and_schema_stable() -> None:
    transport = FixtureTransport({None: (200, page(["2026-08-27"], has_more=True, next_cursor=cursor("2026-08-27"))), "eyJ2IjoxLCJkYXRlIjoiMjAyNi0wOC0yNyJ9": (200, page(["2026-08-28"], request_cursor="2026-08-27"))})
    result = exporter(transport).collect()
    assert not result.errors
    assert len(result.bundle["pages"]) == 2
    assert result.bundle["scope"] == {"startDate": "2024-08-01", "endDateInclusive": "2026-09-03", "priorityDates": list(PRIORITY_DATES)}
    assert result.bundle["schemaVersion"] == EVIDENCE_SCHEMA_VERSION
    assert result.bundle["contract"] == EVIDENCE_CONTRACT
    validate_evidence_bundle(result.bundle)
    assert all("secret" not in json.dumps(receipt).lower() for receipt in result.bundle["pages"])
    assert result.bundle["pages"][0]["usage"]["d1RowsRead"] == 10
    assert result.bundle["pages"][0]["usage"]["d1RowsWritten"] == 0
    assert result.bundle["resourceUsage"]["actual"]["inventoryPages"] == 2
    assert result.bundle["resourceUsage"]["actual"]["responseBytes"] == sum(
        receipt["responseBytes"] for receipt in result.bundle["pages"]
    )
    assert result.bundle["activeReleaseSlots"] == []
    assert transport.requests[0][1]["Authorization"] == "Bearer fixture-ingest"


def test_checked_in_local_budget_matches_exporter_hard_bounds() -> None:
    local = CONFIG["readOnlyInventoryBudget"]
    assert local["workerRequests"] == MAX_TOTAL_REQUESTS
    assert local["r2ClassB"] == MAX_R2_TOTAL_HEADS
    assert local["inventoryPages"] == MAX_PAGES
    assert local["responseBytes"] == MAX_TOTAL_BYTES
    assert local["d1RowsWritten"] == 0
    assert local["r2ClassA"] == 0


def test_fixture_bundle_validates_against_published_json_schema() -> None:
    from jsonschema import Draft202012Validator

    transport = FixtureTransport({None: (200, page(["2026-08-27"]))})
    bundle = exporter(transport).collect().bundle
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(bundle), key=lambda item: list(item.path))
    assert not errors, [error.message for error in errors]


def test_same_fixture_has_stable_plan_hash_on_two_audits() -> None:
    transport = FixtureTransport({None: (200, page(["2026-08-27"]))})
    bundle = exporter(transport).collect().bundle
    first, second_hash = audit_twice(bundle, CONFIG)
    assert first["plan"]["planHash"] == second_hash
    assert first["budget"]["decision"] == "allow"
    assert first["budget"]["evidenceMode"] == "LOCAL_BOUNDED"
    reordered = json.loads(json.dumps(bundle))
    reordered["inventory"] = list(reversed(reordered["inventory"]))
    assert audit_twice(reordered, CONFIG)[0]["plan"]["planHash"] == first["plan"]["planHash"]
    recaptured = dict(bundle)
    recaptured["capturedAt"] = "2026-09-07T02:00:00Z"
    assert audit_twice(recaptured, CONFIG)[0]["plan"]["planHash"] == first["plan"]["planHash"]
    audit_input = to_audit_input(bundle)
    assert audit_input["accountTelemetryEvidence"]["status"] == "UNKNOWN"
    assert audit_input["resourceUsage"]["actual"]["workerRequests"] == 1


@pytest.mark.parametrize("status,code", [(401, "inventory_permission_denied"), (403, "inventory_permission_denied"), (429, "inventory_rate_limited")])
def test_http_permission_and_rate_limit_are_incomplete_not_empty_ready(status: int, code: str) -> None:
    transport = FixtureTransport({None: (status, b"{}")})
    result = exporter(transport).collect()
    assert result.bundle["exportStatus"] == "INCOMPLETE"
    assert result.errors[0]["code"] == code
    assert result.bundle["inventory"] == []
    with pytest.raises(RecoveryEvidenceError):
        RecoveryEvidenceExporter(base_url="https://flow.test", start="2024-08-01", end="2026-09-04")


def test_timeout_and_partial_page_fail_closed() -> None:
    timeout = FixtureTransport({None: RecoveryEvidenceError("inventory_timeout")})
    assert exporter(timeout).collect().bundle["exportStatus"] == "INCOMPLETE"
    # A valid date page can be semantically incomplete (for example, a
    # release manifest is missing) and must remain available to the auditor.
    partial = FixtureTransport({None: (200, page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["RELEASE_MANIFEST_INCOMPLETE"],
    ))})
    result = exporter(partial).collect()
    assert not result.errors
    assert result.bundle["exportStatus"] == "COMPLETE"
    assert len(result.bundle["inventory"]) == 1
    # The pre-fix production route used 503 for this same semantic body; the
    # exporter accepts it for backwards compatibility and still preserves the
    # page-level incomplete status in the captured response hash.
    legacy_partial = FixtureTransport({None: (503, page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["RELEASE_MANIFEST_INCOMPLETE"],
    ))})
    legacy_result = exporter(legacy_partial).collect()
    assert not legacy_result.errors
    assert len(legacy_result.bundle["pages"]) == 1
    transport_failure = FixtureTransport({None: (200, page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["D1_OR_R2_READ_FAILED"],
    ))})
    assert exporter(transport_failure).collect().errors[-1]["code"] == "inventory_incomplete"
    generic_503 = FixtureTransport({None: (503, page(["2026-08-27"]))})
    assert exporter(generic_503).collect().errors[-1]["code"] == "inventory_http_error"


def test_empty_page_and_nonzero_or_unknown_d1_writes_fail_closed() -> None:
    empty = FixtureTransport({None: (200, page([]))})
    assert exporter(empty).collect().errors[-1]["code"] == "inventory_empty_page"
    write_body = json.loads(page(["2026-08-27"]).decode())
    write_body["usage"]["d1"]["rowsWritten"] = 1
    writes = FixtureTransport({None: (200, json.dumps(write_body).encode())})
    assert exporter(writes).collect().errors[-1]["code"] == "inventory_readonly_violation"
    unknown_body = json.loads(page(["2026-08-27"]).decode())
    unknown_body["usage"]["d1"]["rowsWrittenKnown"] = False
    unknown = FixtureTransport({None: (200, json.dumps(unknown_body).encode())})
    assert exporter(unknown).collect().errors[-1]["code"] == "inventory_d1_telemetry_unknown"
    empty_bundle = build_evidence_bundle(
        pages=[], start="2024-08-01", end="2026-09-03", priority_dates=PRIORITY_DATES,
        captured_at=CAPTURED, errors=[{"code": "inventory_timeout", "detail": "timeout"}], request_count=1,
    )
    assert empty_bundle["d1Audit"]["rowsRead"] == "unknown"
    assert empty_bundle["d1Audit"]["rowsWritten"] == "unknown"


def test_cursor_regressions_repeated_page_and_missing_page_are_blocked() -> None:
    nonadvanced = FixtureTransport({
        None: (200, page(["2026-08-27"], has_more=True, next_cursor=cursor("2026-08-27"))),
        "eyJ2IjoxLCJkYXRlIjoiMjAyNi0wOC0yNyJ9": (200, page(["2026-08-28"], request_cursor="2026-08-27", has_more=True, next_cursor=cursor("2026-08-27"))),
    })
    assert exporter(nonadvanced).collect().errors[-1]["code"] == "inventory_cursor_not_advanced"

    first_cursor = cursor("2026-08-28")
    loop = FixtureTransport({
        None: (200, page(["2026-08-28"], has_more=True, next_cursor=first_cursor)),
        first_cursor: (200, page(["2026-08-27"], request_cursor="2026-08-28", has_more=True, next_cursor=cursor("2026-08-27"))),
    })
    assert exporter(loop).collect().errors[-1]["code"] == "inventory_cursor_loop"

    repeated_cursor = cursor("2026-08-27")
    repeated = FixtureTransport({
        None: (200, page(["2026-08-27"], has_more=True, next_cursor=repeated_cursor)),
        repeated_cursor: (200, page(["2026-08-27"], request_cursor="2026-08-27", has_more=True, next_cursor=repeated_cursor)),
    })
    assert exporter(repeated).collect().errors[-1]["code"] == "inventory_repeated_page"

    missing = FixtureTransport({None: (200, page(["2026-08-27"], has_more=True, next_cursor=None))})
    assert exporter(missing).collect().errors[-1]["code"] == "inventory_next_cursor_missing"


def test_r2_head_cursor_is_drained_without_duplicate_date_pages() -> None:
    token = r2_cursor("market-data/releases/2026-08-27/release:a/artifact.json")
    transport = FixtureTransport({
        (None, None): (503, page(
            ["2026-08-27"],
            status="INCOMPLETE",
            incomplete_reasons=["R2_HEAD_PAGE_REMAINS"],
            r2_has_more=True,
            r2_next_cursor=token,
            r2_head_count=64,
        )),
        (None, token): (200, page(
            ["2026-08-27"],
            r2_request_cursor=token,
            r2_has_more=False,
            r2_next_cursor=None,
            r2_head_count=1,
        )),
    })
    result = exporter(transport).collect()
    assert not result.errors
    assert len(result.bundle["pages"]) == 1
    assert result.bundle["runtimeEvidence"]["inventoryTransportPages"] == 2
    assert result.bundle["runtimeEvidence"]["r2HeadPageCount"] == 2
    assert result.bundle["resourceUsage"]["actual"]["workerRequests"] == 2
    assert len(transport.requests) == 2


def test_r2_pagination_allows_known_legacy_warning_but_rejects_fatal_reason() -> None:
    allowed_payload = page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["R2_HEAD_PAGE_REMAINS", "RELEASE_MANIFEST_INCOMPLETE"],
        r2_has_more=True,
        r2_next_cursor=r2_cursor("market-data/releases/2026-08-27/release:a/artifact.json"),
        r2_head_count=64,
    )
    instance = exporter(FixtureTransport({None: (503, allowed_payload)}))
    validated = instance._validate_page(
        status_code=503,
        payload=allowed_payload,
        request_cursor=None,
        request_r2_cursor=None,
    )
    assert validated.body["incompleteReasons"] == ["R2_HEAD_PAGE_REMAINS", "RELEASE_MANIFEST_INCOMPLETE"]

    fatal_payload = page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["R2_HEAD_PAGE_REMAINS", "D1_READ_FAILED"],
        r2_has_more=True,
        r2_next_cursor=r2_cursor("market-data/releases/2026-08-27/release:a/artifact.json"),
        r2_head_count=64,
    )
    with pytest.raises(RecoveryEvidenceError) as error:
        instance._validate_page(
            status_code=503,
            payload=fatal_payload,
            request_cursor=None,
            request_r2_cursor=None,
        )
    assert error.value.code == "inventory_http_error"

    complete_with_fatal_reason = page(
        ["2026-08-27"],
        status="COMPLETE",
        incomplete_reasons=["D1_READ_FAILED"],
    )
    with pytest.raises(RecoveryEvidenceError) as complete_error:
        instance._validate_page(
            status_code=200,
            payload=complete_with_fatal_reason,
            request_cursor=None,
            request_r2_cursor=None,
        )
    assert complete_error.value.code == "inventory_incomplete"

    complete_with_quality_reason = page(
        ["2026-08-27"],
        status="COMPLETE",
        incomplete_reasons=["RELEASE_MANIFEST_INCOMPLETE"],
    )
    with pytest.raises(RecoveryEvidenceError) as complete_quality_error:
        instance._validate_page(
            status_code=200,
            payload=complete_with_quality_reason,
            request_cursor=None,
            request_r2_cursor=None,
        )
    assert complete_quality_error.value.code == "inventory_incomplete"


def test_r2_integrity_error_preserves_bounded_artifact_receipt() -> None:
    key = "market-data/releases/2026-08-27/release:2026-08-27:aaaaaaaaaaaaaaaa/bootstrap.json"
    body = json.loads(page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["R2_HEAD_PAGE_REMAINS", "R2_HASH_MISSING"],
        r2_has_more=True,
        r2_next_cursor=r2_cursor(key),
        r2_head_count=64,
    ).decode())
    body["dates"][0]["releases"]["dashboard"] = [{
        "releaseId": "release:2026-08-27:aaaaaaaaaaaaaaaa",
        "asOf": "2026-08-27",
        "slot": "dashboard",
        "artifacts": {
            "bootstrap": {
                "key": key,
                "declaredSha256": SHA_A,
                "declaredBytes": 100,
                "status": "HASH_MISSING",
                "actualSha256": None,
                "actualBytes": 100,
            },
        },
    }]
    payload = json.dumps(body, separators=(",", ":")).encode()
    instance = exporter(FixtureTransport({None: (503, payload)}))
    with pytest.raises(RecoveryEvidenceError) as error:
        instance._validate_page(
            status_code=503,
            payload=payload,
            request_cursor=None,
            request_r2_cursor=None,
        )
    assert error.value.code == "inventory_http_error"
    assert error.value.evidence is not None
    issue = error.value.evidence["issues"][0]
    assert issue["key"] == key
    assert issue["status"] == "HASH_MISSING"
    assert issue["expectedSha256"] == SHA_A
    assert "nextCursor" not in error.value.evidence.get("r2Pagination", {})

    result = exporter(FixtureTransport({None: (503, payload)})).collect()
    assert result.bundle["exportStatus"] == "INCOMPLETE"
    assert result.errors[0]["evidence"]["issues"][0]["key"] == key
    validate_evidence_bundle(result.bundle)


def test_r2_head_campaign_cap_fails_closed_before_unbounded_drain() -> None:
    pages: dict[tuple[str | None, str | None], tuple[int, bytes]] = {}
    previous: str | None = None
    for index in range(39):
        key = f"market-data/releases/2026-08-27/release:2026-08-27:{index:016x}/artifact.json"
        token = r2_cursor(key)
        pages[(previous, None) if index == 0 else (None, previous)] = (503, page(
            ["2026-08-27"],
            status="INCOMPLETE",
            incomplete_reasons=["R2_HEAD_PAGE_REMAINS"],
            r2_request_cursor=previous,
            r2_has_more=True,
            r2_next_cursor=token,
            r2_head_count=64,
        ))
        previous = token
    pages[(None, previous)] = (503, page(
        ["2026-08-27"],
        status="INCOMPLETE",
        incomplete_reasons=["R2_HEAD_PAGE_REMAINS"],
        r2_request_cursor=previous,
        r2_has_more=True,
        r2_next_cursor=r2_cursor("market-data/releases/2026-08-27/release:2026-08-27:final/artifact.json"),
        r2_head_count=64,
    ))
    transport = FixtureTransport(pages)
    result = exporter(transport).collect()
    assert result.errors[-1]["code"] == "inventory_r2_head_limit"
    assert len(transport.requests) == 39


def test_dashboard_and_penetration_are_independent_and_source_provenance_is_preserved() -> None:
    dashboard = domain_record("2026-08-27", dashboard="READY", penetration="MISSING")
    source_unavailable = domain_record("2026-08-28", raw="SOURCE_UNAVAILABLE")
    source_unavailable["evidence"]["raw"] = [{"sourceBusinessDate": "2026-08-28", "sourceFingerprint": SHA_A, "reason": "official source unavailable", "sourceReadinessCount": 1}]
    source_unavailable["sourceBusinessDate"] = "2026-08-28"
    source_unavailable["sourceFingerprint"] = SHA_A
    source_unavailable["gapReason"] = "official source unavailable"
    source_unavailable["evidence"]["marketHistory"] = True
    bundle = build_evidence_bundle(
        pages=[], start="2024-08-01", end="2026-09-03", priority_dates=PRIORITY_DATES,
        captured_at=CAPTURED, errors=[],
    )
    bundle["inventory"] = [
        {**dashboard, "effectiveDate": "2026-08-27", "required": True, "applicability": "REQUIRED"},
        {**source_unavailable, "effectiveDate": "2026-08-28", "required": True, "applicability": "REQUIRED"},
    ]
    # The v2 validator accepts the explicit fixture, and the v1 adapter keeps
    # the two release slots separate for the existing auditor.
    validate_evidence_bundle(bundle)
    result = audit_bundle(to_audit_input(bundle), budget_config=CONFIG)
    summary = {item["dataDate"]: item for item in result["dateSummaries"]}
    assert summary["2026-08-27"]["stageStatus"]["dashboard"] != summary["2026-08-27"]["stageStatus"]["etf_penetration"]
    assert result["sourceUnavailable"]


def test_nested_source_readiness_evidence_is_promoted_without_downloading_objects() -> None:
    body = json.loads(page(["2026-08-27"]).decode())
    domain = body["dates"][0]["domains"][0]
    domain["stageStatus"]["raw"] = "SOURCE_UNAVAILABLE"
    domain["evidence"]["raw"] = [{
        "status": "error",
        "reason": "official endpoint unavailable",
        "evidence": {
            "sourceBusinessDate": "2026-08-27",
            "sourceFingerprint": SHA_A,
        },
    }]
    result = exporter(FixtureTransport({None: (200, json.dumps(body).encode())})).collect().bundle
    audit_result, _ = audit_twice(result, CONFIG)
    unavailable = [item for item in audit_result["sourceUnavailable"] if item["dataDate"] == "2026-08-27" and item["domain"] == "sse_etf"]
    assert unavailable and unavailable[0]["provenanceStatus"] == "COMPLETE"


def test_usage_missing_expired_mismatched_and_secret_are_unknown(tmp_path: Path) -> None:
    missing, status = load_official_usage(None)
    assert status["status"] == "UNKNOWN"
    assert all(value["value"] == "unknown" for value in missing.values())
    expired_payload = {"capturedAt": "2026-09-01T00:00:00Z", "expiresAt": "2026-09-01T00:30:00Z", "metrics": {field: 1 for field in ("d1RowsReadPerDay", "d1RowsWrittenPerDay", "workersRequestsPerDay", "githubActionsMinutes", "r2StorageBytes", "r2ClassA", "r2ClassB")}}
    expired_payload["sha256"] = canonical_sha256({key: value for key, value in expired_payload.items() if key != "sha256"})
    expired_path = tmp_path / "expired.json"
    expired_path.write_text(json.dumps(expired_payload), encoding="utf-8")
    valid_values, valid = load_official_usage(
        expired_path,
        now=datetime(2026, 9, 1, 0, 15, tzinfo=timezone.utc),
    )
    assert valid["status"] == "VALID" and valid["reason"] == "verified"
    assert valid_values["d1RowsReadPerDay"]["value"] == 1
    values, expired = load_official_usage(expired_path, now=datetime(2026, 9, 7, tzinfo=timezone.utc))
    assert expired["reason"] == "expired" and values["d1RowsReadPerDay"]["value"] == "unknown"
    expired_payload["sha256"] = "0" * 64
    expired_path.write_text(json.dumps(expired_payload), encoding="utf-8")
    _, mismatch = load_official_usage(expired_path, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert mismatch["reason"] == "hash_mismatch"
    secret_path = tmp_path / "secret.json"
    secret_path.write_text(json.dumps({"api_token": "do-not-export", "capturedAt": CAPTURED}), encoding="utf-8")
    _, secret = load_official_usage(secret_path, now=datetime(2026, 9, 7, tzinfo=timezone.utc))
    assert secret["reason"] == "secret_field_rejected"


def test_scope_does_not_expand_to_sep_4() -> None:
    with pytest.raises(RecoveryEvidenceError, match="fixed_scope_required"):
        RecoveryEvidenceExporter(base_url="https://flow.test", end="2026-09-04")


def test_module_has_no_mutation_or_dispatch_capability() -> None:
    source = (ROOT / "src/trading_desk/recovery_evidence.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported.update(alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names)
    assert "subprocess" not in imported
    forbidden_calls = {"post", "put", "patch", "delete", "execute", "dispatch", "insert", "update", "truncate"}
    calls = {node.func.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not calls.intersection(forbidden_calls)
    assert "urlopen" in source
    for forbidden in ("PIPELINE_RECOVERY_ENABLED", "active_release_pointer", "workflow_dispatch(" ):
        assert forbidden not in source
