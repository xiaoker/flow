"""Official-shaped closure evidence is not a production holiday assertion."""
import hashlib
import json

import pytest

from trading_desk.adapters.official import verified_hkex_closures


def closure_manifest(tmp_path, *, fault=None):
    day = "2026-07-16"
    rows = [{"date": day, "market": market, "tradingDay": 0} for market in (
        "SSE Northbound", "SZSE Northbound", "SSE Southbound", "SZSE Southbound")]
    if fault == "missing_channel": rows.pop()
    if fault == "duplicate": rows[-1] = rows[0].copy()
    if fault == "stale": rows[0]["date"] = "2026-07-15"
    if fault == "open": rows[0]["tradingDay"] = 1
    if fault == "string": rows[0]["tradingDay"] = "0"
    if fault == "bool": rows[0]["tradingDay"] = False
    if fault == "missing_flag": rows[0].pop("tradingDay")
    if fault == "empty": rows = []
    content = ("tabData = " + json.dumps(rows) + ";").encode()
    path = tmp_path / "raw.js"
    path.write_bytes(content)
    artifact = {"source": {"source_id": "hkex", "dataset": "stock_connect_daily"},
                "requested_date": day, "status_code": 200, "raw_path": path.name,
                "sha256": hashlib.sha256(content).hexdigest()}
    return {"requested_dates": [day], "artifacts": [artifact]}


@pytest.mark.parametrize("fault", [None, "missing_channel", "duplicate", "stale", "open", "string", "bool", "missing_flag", "empty"])
def test_closure_requires_exact_four_channel_date_and_integer_flags(tmp_path, fault):
    manifest = closure_manifest(tmp_path, fault=fault)
    result = verified_hkex_closures(manifest, tmp_path)
    assert bool(result) is (fault is None)
    if result:
        assert result == {"2026-07-16": manifest["artifacts"][0]["sha256"]}


def test_closure_rejects_bad_hash_and_path(tmp_path):
    manifest = closure_manifest(tmp_path)
    manifest["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash_mismatch"):
        verified_hkex_closures(manifest, tmp_path)


def test_conflicting_observation_does_not_authorize_closure(tmp_path):
    manifest = closure_manifest(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    open_manifest = closure_manifest(other, fault="open")
    open_manifest["artifacts"][0]["raw_path"] = "other/raw.js"
    manifest["artifacts"].extend(open_manifest["artifacts"])
    assert verified_hkex_closures(manifest, tmp_path) == {}
    manifest["artifacts"][0]["raw_path"] = "../outside.js"
    with pytest.raises(ValueError, match="path_invalid"):
        verified_hkex_closures(manifest, tmp_path)


@pytest.mark.parametrize("whole_window", [False, True])
def test_closed_source_end_to_end_keeps_missing_metrics_not_zero(tmp_path, monkeypatch, whole_window):
    from test_phase0_archive import fixture_recipe, DATES
    from trading_desk.phase0_archive import ArchivedOfficialMarketAdapters
    from trading_desk.phase0 import collect_phase0
    from trading_desk.dashboard import build_dashboard_snapshot
    from trading_desk import adapters
    from trading_desk.source_readiness import build_source_readiness
    from trading_desk.release_qa import run_release_qa
    recipe, bodies = fixture_recipe(closed_days=DATES if whole_window else [DATES[-1]])
    adapter = ArchivedOfficialMarketAdapters(recipe, target=DATES[-1], root=tmp_path / "archive",
        fetch=lambda ref: bodies[ref["objectKey"]])
    result = collect_phase0(dates=DATES, data_root=tmp_path / "data", report_root=tmp_path / "reports",
        workspace_root=tmp_path, collection_window="reconcile", archived_adapters=adapter)
    manifest_path = tmp_path / "manifest.json"
    quality_path = tmp_path / "quality.json"
    manifest_path.write_text(json.dumps(result["manifest"]))
    quality_path.write_text(json.dumps(result["quality"]))
    readiness = build_source_readiness(result["manifest"], result["quality"])
    row = next(row for row in readiness["sources"] if row["sourceId"] == "stock_connect_summary")
    assert row["status"] == "ready" and row["reason"] == "official_source_target_date_closed"
    assert row["observedDates"] == ([] if whole_window else DATES[:-1])
    # Removing closure recognition reproduces the original false pending state.
    with monkeypatch.context() as context:
        context.setattr(adapters.official, "verified_hkex_closures", lambda *args: {})
        before = build_dashboard_snapshot(manifest_path=manifest_path, quality_path=quality_path, workspace_root=tmp_path)
        assert before["publication"]["status"] == "pending"
        assert "港交所互联互通" in before["publication"]["pendingDomains"]
    snapshot = build_dashboard_snapshot(manifest_path=manifest_path, quality_path=quality_path, workspace_root=tmp_path)
    assert snapshot["asOf"] == DATES[-1] and snapshot["publication"]["status"] == "confirmed"
    assert all(row["value"] == "—" for row in snapshot["metrics"] if row["id"] in {"southbound", "northbound"})
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot))
    report = run_release_qa(manifest_path=manifest_path, quality_path=quality_path, snapshot_path=snapshot_path,
        output_path=tmp_path / "qa.json", workspace_root=tmp_path)
    assert report["counts"]["fail"] == 0, [row for row in report["findings"] if row["status"] == "fail"]
