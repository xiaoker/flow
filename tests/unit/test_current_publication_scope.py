"""Historical source failures cannot masquerade as current facts or stop valid days."""
import json
from pathlib import Path
import subprocess

import pytest
import requests

from trading_desk import phase0
from trading_desk.adapters import official
from trading_desk.dashboard import build_dashboard_snapshot
from trading_desk.phase0_archive import ArchivedOfficialMarketAdapters
from trading_desk.release_qa import run_release_qa
from trading_desk.source_readiness import build_source_readiness
from test_phase0_archive import DATES, fixture_recipe

ROOT = Path(__file__).resolve().parents[2]


def collect_with_fault(tmp_path, monkeypatch, *, day=None, window="reconcile", archive=False, unknown=False, partial_current=False, opt_in=True):
    recipe, bodies = fixture_recipe()
    adapter = ArchivedOfficialMarketAdapters(recipe, target=DATES[-1], root=tmp_path / "archive",
        fetch=lambda ref: bodies[ref["objectKey"]])
    original = adapter.fetch_hkex_connect
    live = official.OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="rejected")
    response = requests.Response()
    response.status_code = 200
    response._content = b"\x00" * 9110
    response.headers["Content-Type"] = "application/javascript"
    monkeypatch.setattr(official, "get", lambda *a, **kw: response)

    def fetch(date):
        if date == day:
            if unknown:
                raise ValueError("raw_sha256_mismatch")
            return live.fetch_hkex_connect(date)
        result = original(date)
        if partial_current and date == DATES[-1]:
            summary, top, artifact = result
            return summary.iloc[:-1].copy(), top, artifact
        return result

    monkeypatch.setattr(adapter, "fetch_hkex_connect", fetch)
    monkeypatch.setattr(phase0, "OfficialMarketAdapters", lambda **kwargs: adapter)
    return phase0.collect_phase0(dates=DATES, data_root=tmp_path / "data", report_root=tmp_path / "reports",
        workspace_root=tmp_path, collection_window=window, archived_adapters=adapter if archive else None,
        isolate_historical_hkex_errors=opt_in)


def materialize_and_qa(tmp_path, result):
    manifest = tmp_path / "manifest.json"
    quality = tmp_path / "quality.json"
    manifest.write_text(json.dumps(result["manifest"]))
    quality.write_text(json.dumps(result["quality"]))
    snapshot = build_dashboard_snapshot(manifest_path=manifest, quality_path=quality, workspace_root=tmp_path)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    qa = run_release_qa(manifest_path=manifest, quality_path=quality, snapshot_path=path,
        output_path=tmp_path / "qa.json", workspace_root=tmp_path)
    return snapshot, qa


def failure_shell(tmp_path, quality):
    path = tmp_path / "reports/phase0/latest-quality-summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(quality))
    workflow = (ROOT / ".github/workflows/market-data-collect.yml").read_text()
    step = workflow.split("      - name: Fail loudly on unresolved source errors\n", 1)[1].split("\n      - name:", 1)[0]
    shell = "\n".join(line[10:] for line in step.split("        run: |\n", 1)[1].splitlines())
    shell = shell.replace("${{ steps.window.outputs.collection_window }}", "reconcile").replace(
        "${{ steps.collection.outputs.pending }}", "false")
    return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell], cwd=tmp_path,
        capture_output=True, text=True)


@pytest.mark.parametrize("missing_day", DATES[:-1])
def test_historical_hkex_rejected_response_preserves_gap_but_publishes_valid_current_day(tmp_path, monkeypatch, missing_day):
    result = collect_with_fault(tmp_path, monkeypatch, day=missing_day)
    quality = result["quality"]
    assert quality["phase0_gate_status"] == "conditional_pass"
    assert len(quality["errors"]) == 1  # never delete/relabel the source failure
    assert quality["errors"][0]["error_type"] == "HkexResponseParseError"
    assert "nul_only" in quality["errors"][0]["message"]
    assert quality["publication_errors"] == []
    assert "stock_connect_summary" not in quality["pending_domains"]
    ledger = build_source_readiness(result["manifest"], quality)
    connect = next(row for row in ledger["sources"] if row["sourceId"] == "stock_connect_summary")
    assert connect["status"] == "ready"
    assert connect["evidence"]["historicalErrorDates"] == [missing_day]
    snapshot, qa = materialize_and_qa(tmp_path, result)
    assert snapshot["asOf"] == DATES[-1] and snapshot["publication"]["status"] == "confirmed"
    assert qa["counts"]["fail"] == 0, [f for f in qa["findings"] if f["status"] == "fail"]
    gap = next(f for f in qa["findings"] if f["check_id"] == "mapping.connect_channels")
    assert gap["status"] == "warn" and gap["evidence"]["missing_dates"] == [missing_day]
    assert failure_shell(tmp_path, quality).returncode == 0
    # No synthetic day or zero is inserted into history; independent current
    # headlines are computed from the real four-channel target rows.
    assert [row["date"] for row in snapshot["connectSeries"]] == [d[5:] for d in DATES if d != missing_day]
    south = next(row for row in snapshot["metrics"] if row["id"] == "southbound")
    assert float(south["value"]) == pytest.approx(0.4)


@pytest.mark.parametrize("fault", ["current", "unknown", "legacy", "archive", "runner_default"])
def test_unscoped_current_and_identity_errors_still_block(tmp_path, monkeypatch, fault):
    result = collect_with_fault(tmp_path, monkeypatch,
        day=DATES[-1] if fault == "current" else DATES[1],
        window="auto" if fault == "legacy" else "reconcile",
        archive=fault == "archive", unknown=fault == "unknown", opt_in=fault != "runner_default")
    quality = result["quality"]
    assert quality["phase0_gate_status"] == "blocked"
    assert len(quality["publication_errors"]) == 1
    assert failure_shell(tmp_path, quality).returncode != 0
    ledger = build_source_readiness(result["manifest"], quality)
    row = next(r for r in ledger["sources"] if r["sourceId"] == "stock_connect_summary")
    assert row["status"] == "error"  # HKEX step alias must not turn a parser error into pending


def test_projection_tampering_and_current_channel_loss_are_not_release_permissions(tmp_path, monkeypatch):
    result = collect_with_fault(tmp_path, monkeypatch, day=DATES[1])
    result["quality"]["publication_scope"]["target_date"] = DATES[1]
    assert phase0.publication_blocking_errors(result["quality"]) == result["quality"]["errors"]
    _, qa = materialize_and_qa(tmp_path, result)
    assert qa["counts"]["fail"] > 0


def test_unknown_error_cannot_be_hidden_by_an_empty_publication_projection(tmp_path, monkeypatch):
    result = collect_with_fault(tmp_path, monkeypatch, day=DATES[1], unknown=True)
    result["quality"]["publication_errors"] = []
    result["quality"]["phase0_gate_status"] = "conditional_pass"
    _, qa = materialize_and_qa(tmp_path, result)
    assert any(f["status"] == "fail" and f["check_id"] == "publication.base_quality_gate" for f in qa["findings"])


def test_etf_previous_day_baseline_error_remains_blocking(tmp_path, monkeypatch):
    recipe, bodies = fixture_recipe()
    adapter = ArchivedOfficialMarketAdapters(recipe, target=DATES[-1], root=tmp_path / "archive",
        fetch=lambda ref: bodies[ref["objectKey"]])
    original = adapter.fetch_sse_etf

    def fetch(day):
        if day == DATES[-2]:
            raise ValueError("ETF baseline source invalid")
        return original(day)

    monkeypatch.setattr(adapter, "fetch_sse_etf", fetch)
    monkeypatch.setattr(phase0, "OfficialMarketAdapters", lambda **kw: adapter)
    result = phase0.collect_phase0(dates=DATES, data_root=tmp_path / "data", report_root=tmp_path / "reports",
        workspace_root=tmp_path, collection_window="reconcile", isolate_historical_hkex_errors=True)
    assert result["quality"]["phase0_gate_status"] == "blocked"
    assert len(result["quality"]["publication_errors"]) == 1
    snapshot, _ = materialize_and_qa(tmp_path, result)
    assert snapshot["publication"]["status"] == "pending"


def test_raw_hash_still_fails_independent_qa(tmp_path, monkeypatch):
    result = collect_with_fault(tmp_path, monkeypatch, day=DATES[1])
    result["manifest"]["artifacts"][0]["sha256"] = "0" * 64
    _, qa = materialize_and_qa(tmp_path, result)
    assert any(f["status"] == "fail" and f["check_id"].startswith("lineage.") for f in qa["findings"])


def test_missing_current_channel_still_blocks_snapshot_and_qa(tmp_path, monkeypatch):
    import pandas as pd

    result = collect_with_fault(tmp_path, monkeypatch, day=DATES[1])
    path = Path(result["manifest"]["combined_paths"]["stock_connect_summary"])
    path = path if path.is_absolute() else tmp_path / path
    frame = pd.read_csv(path)
    frame = frame[~(frame["trade_date"].eq(DATES[-1]) & frame["market"].eq("SZSE") & frame["direction"].eq("southbound"))]
    frame.to_csv(path, index=False)
    snapshot, qa = materialize_and_qa(tmp_path, result)
    assert snapshot["publication"]["status"] == "pending"
    assert any(f["status"] == "fail" and f["check_id"] == "mapping.connect_channels" for f in qa["findings"])


def test_partial_current_channels_cannot_be_marked_ready(tmp_path, monkeypatch):
    result = collect_with_fault(tmp_path, monkeypatch, day=DATES[1], partial_current=True)
    assert result["quality"]["phase0_gate_status"] == "blocked"
    ledger = build_source_readiness(result["manifest"], result["quality"])
    row = next(r for r in ledger["sources"] if r["sourceId"] == "stock_connect_summary")
    assert row["status"] == "error"


def test_workflow_uses_publication_projection_with_legacy_fallback():
    source = (ROOT / ".github/workflows/market-data-collect.yml").read_text()
    assert source.count(".publication_errors // .errors") == 5
    assert "(.errors | length > 0) or (.phase0_gate_status" not in source


@pytest.mark.parametrize("window", ["auto", "evening", "morning", "reconcile"])
def test_only_ordinary_full_source_cli_explicitly_opts_in(monkeypatch, capsys, window):
    from trading_desk import cli

    calls = []

    def collect(**kwargs):
        calls.append(kwargs)
        return {"manifest": {"run_id": "fixture", "requested_dates": DATES},
                "quality": {"counts": {}, "phase0_gate_status": "pass", "phase0_gate_passed": True, "errors": []}}

    monkeypatch.setattr(cli, "collect_phase0", collect)
    monkeypatch.setattr("sys.argv", ["trading-desk", "collect-phase0", "--dates", *DATES, "--window", window])
    cli.main()
    assert calls[0]["isolate_historical_hkex_errors"] is (window in {"morning", "reconcile"})
    assert json.loads(capsys.readouterr().out)["errors"] == 0
