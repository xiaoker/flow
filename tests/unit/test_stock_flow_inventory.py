from __future__ import annotations

from pathlib import Path

import pytest

from scripts.inventory_stock_flow_inputs import _dates, _exact_slots
from scripts.select_market_history_dates import _explicit_dates, _validated_range


def test_inventory_dates_are_sorted_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-08-18\n2026-08-17\n2026-08-18\n", encoding="utf-8")
    assert _dates(path) == ["2026-08-17", "2026-08-18"]


def test_inventory_rejects_non_iso_dates(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-08-18\nnot-a-date\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ISO date"):
        _dates(path)


def test_inventory_date_list_is_calendar_valid_and_limited_to_five(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-02-30\n", encoding="utf-8")
    with pytest.raises(ValueError, match="valid exact ISO"):
        _dates(path)
    path.write_text("".join(f"2026-09-{day:02}\n" for day in range(1, 7)), encoding="utf-8")
    with pytest.raises(ValueError, match="limited to 5"):
        _dates(path)


def test_explicit_inventory_dates_are_sorted_validated_and_bounded() -> None:
    assert _explicit_dates("2026-09-03\n2026-09-01\n") == ["2026-09-01", "2026-09-03"]
    with pytest.raises(ValueError, match="duplicates"):
        _explicit_dates("2026-09-01\n2026-09-01\n")
    with pytest.raises(ValueError, match="limited to 5"):
        _explicit_dates("".join(f"2026-09-{day:02}\n" for day in range(1, 7)))
    with pytest.raises(ValueError):
        _explicit_dates("2026-02-30\n")


def test_market_history_range_is_exact_and_limited_to_seven_calendar_days() -> None:
    assert _validated_range("2026-09-01", "2026-09-07")[1].isoformat() == "2026-09-07"
    with pytest.raises(ValueError, match="must not exceed 7"):
        _validated_range("2026-09-01", "2026-09-08")
    with pytest.raises(ValueError):
        _validated_range("2026-09-01; DROP TABLE x", "2026-09-03")


def test_bounded_d1_date_query_has_an_existing_trade_date_index() -> None:
    import sqlite3
    root = Path(__file__).resolve().parents[2]
    migration = root / "web/drizzle/0003_market_history_daily.sql"
    database = sqlite3.connect(":memory:")
    for statement in migration.read_text(encoding="utf-8").split("--> statement-breakpoint"):
        if statement.strip():
            database.executescript(statement)
    plan = database.execute(
        "EXPLAIN QUERY PLAN SELECT trade_date FROM market_history_daily "
        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date ASC LIMIT 6",
        ("2026-09-01", "2026-09-07"),
    ).fetchall()
    assert any("market_history_daily_date_idx" in str(row) or "PRIMARY KEY" in str(row) for row in plan)


def test_inventory_retry_count_is_hard_capped_before_network(tmp_path: Path, monkeypatch) -> None:
    import sys
    from scripts import inventory_stock_flow_inputs as inventory
    dates = tmp_path / "dates.txt"
    dates.write_text("2026-09-01\n", encoding="utf-8")
    monkeypatch.setattr(inventory.requests, "Session", lambda: pytest.fail("invalid retry bound must fail before network"))
    monkeypatch.setattr(sys, "argv", ["inventory", "--base-url", "https://example.test", "--dates", str(dates),
        "--output", str(tmp_path / "report.json"), "--attempts", "3"])
    with pytest.raises(ValueError, match="between 1 and 2"):
        inventory.main()


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
    assert all(c["strict_selection"] is True for c in calls)


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
        def head(self, endpoint, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert "stream" not in kwargs
            return reply
    assert _probe(Session(), endpoint="https://flow.xiaoker.com", date_value="2026-09-23",
        source="sse", dataset="etf_scale", headers={}, attempts=1)["status"] == "ready"
    assert reply.closed


@pytest.mark.parametrize("status", ["error", "invalid_metadata", "ambiguous"])
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


@pytest.mark.parametrize("case,expected", [
    ("indexed", "ready"), ("single", "ready"), ("latest", "invalid_metadata"),
    ("missing_revision", "invalid_metadata"),
    ("wrong_date", "invalid_metadata"), ("wrong_key", "invalid_metadata"),
    ("ambiguous", "ambiguous"), ("missing", "missing"),
])
def test_exact_probe_uses_formal_selection_without_reading_body(case, expected):
    from scripts.inventory_stock_flow_inputs import _probe
    headers = {"x-flow-normalized-sha256": "a" * 64,
        "x-flow-normalized-key": "market-data/normalized/szse/etf_scale/2026-09-21/" + "b" * 64 + "/data.csv",
        "x-flow-trade-date": "2026-09-21",
        # Pointer revisions are opaque strings, not guaranteed content hashes.
        "x-flow-pcf-revision": "etf_scale:szse:2026-09-21:be073bc49af33f36"}
    if case in ("single", "latest"):
        headers.update({"x-flow-normalized-selection": "single" if case == "single" else "latest-uploaded",
            "x-flow-normalized-candidate-count": "1" if case == "single" else "2"})
        headers.pop("x-flow-pcf-revision")
    if case == "missing_revision":
        headers.pop("x-flow-pcf-revision")
    if case == "wrong_date":
        headers["x-flow-trade-date"] = "2026-09-20"
    if case == "wrong_key":
        headers["x-flow-normalized-key"] = headers["x-flow-normalized-key"].replace("szse", "sse")
    class Reply:
        status_code = 409 if case == "ambiguous" else 404 if case == "missing" else 200
        ok = status_code == 200
        closed = False
        @property
        def content(self):
            pytest.fail("metadata probe must never read response bodies")
        def close(self):
            self.closed = True
    reply = Reply()
    reply.headers = headers
    class Session:
        calls = 0
        def head(self, endpoint, **kwargs):
            self.calls += 1
            assert kwargs["params"]["selection"] == "indexed-or-single"
            assert not kwargs["allow_redirects"]
            return reply
    session = Session()
    result = _probe(session, endpoint="https://flow.xiaoker.com", date_value="2026-09-21",
        source="szse", dataset="etf_scale", headers={}, attempts=1, strict_selection=True)
    assert result["status"] == expected
    if expected == "ready":
        assert result["selection"] == ("indexed" if case == "indexed" else "single")
        assert result["candidateCount"] == (None if case == "indexed" else 1)
    assert session.calls == 1 and reply.closed


def test_pcf_inventory_requires_a_single_or_indexed_candidate() -> None:
    from scripts.inventory_stock_flow_inputs import _probe
    key = "market-data/normalized/sse/etf_pcf/2026-09-21/" + "b" * 64 + "/data.csv.gz"
    class Reply:
        status_code = 200
        ok = True
        headers = {
            "x-flow-normalized-sha256": "a" * 64,
            "x-flow-normalized-key": key,
            "x-flow-normalized-candidate-count": "1",
            "x-flow-normalized-selection": "single",
            "x-flow-trade-date": "2026-09-21",
        }
        def close(self): pass
    class Session:
        def head(self, endpoint, **kwargs):
            assert kwargs["params"]["selection"] == "indexed-or-single"
            return Reply()
    result = _probe(Session(), endpoint="https://flow.xiaoker.com", date_value="2026-09-21",
        source="sse", dataset="etf_pcf", headers={}, attempts=1, strict_selection=True)
    assert result["status"] == "ready"
    assert result["selection"] == "single"


def test_exact_workflow_skips_d1_and_wranger_while_explicit_dates_skip_cli() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/stock-flow-inventory.yml").read_text()
    for name in ("Install Wrangler", "Query bounded D1 dates"):
        step = workflow.split(f"- name: {name}", 1)[1].split("- name:", 1)[0]
        assert "if: ${{ inputs.exact_slots_json == '' && inputs.probe_dates == '' }}" in step
    explicit_step = workflow.split("- name: Resolve explicitly supplied dates", 1)[1].split("- name:", 1)[0]
    assert "if: ${{ inputs.exact_slots_json == '' && inputs.probe_dates != '' }}" in explicit_step
    assert "CLOUDFLARE_API_TOKEN" not in explicit_step
    step = workflow.split("- name: Probe archived normalized metadata only", 1)[1]
    assert 'EXACT_SLOTS_JSON: ${{ inputs.exact_slots_json }}' in step
    assert '--slots-json "$EXACT_SLOTS_JSON"' in step
    assert "--attempts 1" in step
    assert 'PROBE_DATES: ${{ inputs.probe_dates }}' in step
    conflict = workflow.split("- name: Reject conflicting inventory inputs", 1)[1].split("- name:", 1)[0]
    assert "EXACT_SLOTS_JSON" in conflict and "PROBE_DATES" in conflict


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
