from __future__ import annotations

from pathlib import Path

import pytest

from scripts.inventory_stock_flow_inputs import _dates, _exact_slots


def test_inventory_dates_are_sorted_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-08-18\n2026-08-17\n2026-08-18\n", encoding="utf-8")
    assert _dates(path) == ["2026-08-17", "2026-08-18"]


def test_inventory_rejects_non_iso_dates(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-08-18\nnot-a-date\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ISO date"):
        _dates(path)


@pytest.mark.parametrize("value", ["[]", "{}", '[{"date":"2026-02-30","source":"sse"}]',
    '[{"date":"2026-09-23","source":"other"}]',
    '[{"date":"2026-09-23","source":"sse","dataset":"etf_pcf"}]',
    '[{"date":"2026-09-23","source":"sse"},{"date":"2026-09-23","source":"sse"}]'])
def test_exact_inventory_rejects_invalid_or_expanded_scope(value: str) -> None:
    with pytest.raises(ValueError):
        _exact_slots(value)


def test_exact_inventory_has_six_slot_hard_cap() -> None:
    import json
    with pytest.raises(ValueError):
        _exact_slots(json.dumps([{"date": f"2026-09-{day:02}", "source": "sse"} for day in range(1, 8)]))


def test_exact_inventory_probes_only_requested_slots_without_dates_or_pcf(tmp_path, monkeypatch) -> None:
    import json
    import sys
    from scripts import inventory_stock_flow_inputs as inventory

    calls = []
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-not-a-secret")
    def probe(session, **kwargs):
        calls.append(kwargs)
        return {"status": "missing", "code": 404}
    monkeypatch.setattr(inventory, "_probe", probe)
    monkeypatch.setattr(inventory, "_dates", lambda path: pytest.fail("exact scope must not resolve date inventory"))
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["inventory", "--base-url", "https://flow.xiaoker.com", "--slots-json",
        '[{"date":"2026-09-23","source":"szse"},{"date":"2026-09-23","source":"sse"}]',
        "--output", str(output), "--attempts", "1"])
    inventory.main()
    assert [(c["source"], c["dataset"], c["attempts"]) for c in calls] == [("sse", "etf_scale", 1), ("szse", "etf_scale", 1)]
    assert len(json.loads(output.read_text())["slots"]) == 2


def test_probe_does_not_follow_redirects_or_read_response_body() -> None:
    from scripts.inventory_stock_flow_inputs import _probe
    class Reply:
        ok = True
        status_code = 200
        headers = {"x-flow-normalized-sha256": "a" * 64, "x-flow-normalized-key": "exact-key"}
        closed = False
        @property
        def content(self):
            pytest.fail("metadata inventory must not read payload")
        def close(self):
            self.closed = True
    reply = Reply()
    class Session:
        def get(self, endpoint, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert kwargs["stream"] is True
            return reply
    assert _probe(Session(), endpoint="https://flow.xiaoker.com", date_value="2026-09-23",
        source="sse", dataset="etf_scale", headers={}, attempts=1)["status"] == "ready"
    assert reply.closed


@pytest.mark.parametrize("status", ["error", "invalid_metadata"])
def test_exact_inventory_stops_on_unknown_and_preserves_unprobed_scope(tmp_path, monkeypatch, status) -> None:
    import json
    import sys
    from scripts import inventory_stock_flow_inputs as inventory
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-not-a-secret")
    calls = []
    def probe(session, **kwargs):
        calls.append(kwargs)
        return {"status": status, "code": 401}
    monkeypatch.setattr(inventory, "_probe", probe)
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["inventory", "--base-url", "https://flow.xiaoker.com", "--slots-json",
        '[{"date":"2026-09-23","source":"sse"},{"date":"2026-09-24","source":"szse"}]',
        "--output", str(output), "--attempts", "1"])
    inventory.main()
    report = json.loads(output.read_text())
    assert len(calls) == 1
    assert report["complete"] is False
    assert len(report["requestedSlots"]) == 2
    assert len(report["slots"]) == 1


def test_exact_workflow_skips_date_scan_and_pcf_without_shell_interpolation() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/stock-flow-inventory.yml").read_text()
    for name in ("Install Wrangler", "Resolve official market-history dates"):
        step = workflow.split(f"- name: {name}", 1)[1].split("- name:", 1)[0]
        assert "if: ${{ inputs.exact_slots_json == '' }}" in step
    step = workflow.split("- name: Probe archived normalized metadata only", 1)[1]
    assert 'EXACT_SLOTS_JSON: ${{ inputs.exact_slots_json }}' in step
    assert '--slots-json "$EXACT_SLOTS_JSON"' in step
    assert "--attempts 1" in step
    assert step.index("exit 0") < step.index("--dates reports/")


@pytest.mark.parametrize("origin,authenticated", [("https://example.com", True), ("https://flow.xiaoker.com", False)])
def test_exact_inventory_rejects_wrong_origin_or_missing_auth_before_request(tmp_path, monkeypatch, origin, authenticated) -> None:
    import sys
    from scripts import inventory_stock_flow_inputs as inventory
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        if authenticated:
            monkeypatch.setenv(name, "fixture-not-a-secret")
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(inventory.requests, "Session", lambda: pytest.fail("preflight must reject before session creation"))
    monkeypatch.setattr(sys, "argv", ["inventory", "--base-url", origin, "--slots-json",
        '[{"date":"2026-09-23","source":"sse"}]', "--output", str(tmp_path / "report.json"), "--attempts", "1"])
    with pytest.raises(ValueError):
        inventory.main()
