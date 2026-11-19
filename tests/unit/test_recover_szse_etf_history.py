from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import recover_szse_etf_history as history
from scripts.recover_szse_etf_history import RequestBudget


def test_request_budget_counts_each_top_level_request_once() -> None:
    budget = RequestBudget(maximum=2, min_interval_seconds=0)

    budget.before_request()
    budget.before_request()

    assert budget.request_count == 2
    with pytest.raises(RuntimeError, match="recovery_request_budget_exceeded"):
        budget.before_request()
    assert budget.request_count == 2


def test_request_budget_paces_concurrent_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr("scripts.recover_szse_etf_history.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("scripts.recover_szse_etf_history.time.sleep", sleep)
    budget = RequestBudget(maximum=3, min_interval_seconds=2)

    budget.before_request()
    budget.before_request()
    budget.before_request()

    assert sleeps == [2.0, 2.0]
    assert budget.request_count == 3


@pytest.mark.parametrize(
    ("maximum", "interval"),
    [(0, 0), (-1, 0), (1, -0.1), (1, float("nan"))],
)
def test_request_budget_rejects_invalid_bounds(maximum: int, interval: float) -> None:
    with pytest.raises(ValueError):
        RequestBudget(maximum=maximum, min_interval_seconds=interval)


def test_szse_history_request_falls_back_to_second_official_host(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return history.subprocess.CompletedProcess(command, 22, stdout="", stderr="")
        Path(command[command.index("--output") + 1]).write_text(json.dumps([]))
        return history.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(history.subprocess, "run", run)
    monkeypatch.setattr(history, "_require_bounded_curl", lambda: None)
    budget = RequestBudget(maximum=2, min_interval_seconds=0)
    payload = history._request_json(
        history._session(), {"SHOWTYPE": "JSON", "txtDm": "159915"}, budget=budget
    )

    assert payload == []
    assert [call[0][-1] for call in calls] == [
        history.WWW_REPORT_URL,
        history.REPORT_URL,
    ]
    assert budget.active_host == "fund.szse.cn"
    assert budget.request_count == 2
    assert budget.response_bytes == 2
    assert len(budget.pending_responses) == 1  # failed first host is not zero cost
    assert all("--max-filesize" in call[0] for call in calls)
    assert all(call[1]["timeout"] <= 30 for call in calls)


def test_szse_history_request_rejects_invalid_configured_host(monkeypatch) -> None:
    monkeypatch.setenv(history.SZSE_HOST_ENV, "evil.example")
    budget = RequestBudget(maximum=2, min_interval_seconds=0)
    with pytest.raises(ValueError, match="invalid_szse_host"):
        budget.host_order()
    assert budget.request_count == 0


@pytest.mark.parametrize('kwargs', [
    {'max_duration_seconds': 0}, {'max_duration_seconds': float('nan')},
    {'max_response_bytes': 0}, {'max_response_bytes': True},
    {'max_response_bytes': 10, 'max_total_response_bytes': 9},
    {'max_total_cached_bytes': 0}, {'max_total_cached_bytes': True},
])
def test_invalid_transport_bounds(kwargs):
    with pytest.raises(ValueError):
        RequestBudget(10, 0, **kwargs)


def test_deadline_does_not_sleep_or_dispatch_past_expiry(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(history.time, 'monotonic', lambda: clock[0])
    budget = RequestBudget(10, 2, max_duration_seconds=1)
    budget.before_request()
    monkeypatch.setattr(history.time, 'sleep', lambda _: pytest.fail('would exceed deadline'))
    with pytest.raises(RuntimeError, match='deadline'):
        budget.before_request()
    clock[0] = 2
    with pytest.raises(RuntimeError, match='deadline'):
        budget.before_request()
    assert budget.request_count == 1


def test_response_reservations_prevent_concurrent_overcommit():
    budget = RequestBudget(10, 0, max_response_bytes=10, max_total_response_bytes=20)
    a = budget.before_request()
    budget.before_request()  # unknown failure keeps this reservation
    with pytest.raises(RuntimeError, match='response_budget'):
        budget.before_request()
    budget.finish_response(a, 3)
    with pytest.raises(RuntimeError, match='response_budget'):
        budget.before_request()  # 3 known + 10 unknown + 10 proposed >20
    assert budget.response_bytes == 3 and len(budget.pending_responses) == 1
    with pytest.raises(RuntimeError, match='accounting'):
        budget.finish_response(a, 0)


def test_cached_file_reads_have_an_aggregate_bound(tmp_path):
    path = tmp_path / 'cached.json'
    body = {'request': {'code': '159915', 'start': '2026-08-28', 'end': '2026-09-28'},
            'source_url': history.REPORT_URL, 'response': []}
    path.write_text(json.dumps(body))
    size = path.stat().st_size
    budget = RequestBudget(10, 0, max_response_bytes=1000, max_total_cached_bytes=size)
    assert history._load_cached_history(path, '159915', '2026-08-28', '2026-09-28', 1000,
                                        budget=budget) == body
    assert budget.cached_bytes == size
    with pytest.raises(RuntimeError, match='cache_budget_exceeded'):
        history._load_cached_history(path, '159915', '2026-08-28', '2026-09-28', 1000,
                                     budget=budget)
    assert budget.cached_bytes == size


def test_output_root_lock_rejects_a_second_invocation_before_work(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['recover', '--output-root', str(tmp_path)])
    with history._exclusive_output_lock(tmp_path):
        with pytest.raises(RuntimeError, match='output_already_in_use'):
            history.main()
    assert not list(tmp_path.glob('request-budget-*.json'))
    with history._exclusive_output_lock(tmp_path):
        pass


def test_cache_contract_binds_dates_codes_and_preserves_old_files(tmp_path):
    (tmp_path / 'responses').mkdir()
    dates = ['2026-08-28', '2026-09-01']
    history._cache_contract(tmp_path, ['159915'], dates)
    before = (tmp_path / 'cache-contract.json').read_bytes()
    history._cache_contract(tmp_path, ['159915'], dates)
    for codes, changed in [(['159919'], dates), (['159915'], ['2026-09-02'])]:
        with pytest.raises(ValueError, match='contract_mismatch'):
            history._cache_contract(tmp_path, codes, changed)
    assert (tmp_path / 'cache-contract.json').read_bytes() == before


def test_legacy_cache_without_contract_fails_closed(tmp_path):
    (tmp_path / 'responses').mkdir()
    old = tmp_path / 'responses/159915.json'
    old.write_text('{}')
    with pytest.raises(ValueError, match='contract_missing'):
        history._cache_contract(tmp_path, ['159915'], ['2026-09-01'])
    assert old.read_text() == '{}' and not (tmp_path / 'cache-contract.json').exists()


@pytest.mark.parametrize('field,value', [
    ('request', {'code': '159919', 'start': '2026-08-28', 'end': '2026-09-28'}),
    ('request', {'code': '159915', 'start': '2026-09-01', 'end': '2026-09-28'}),
    ('source_url', 'https://evil.example'), ('response', {}),
])
def test_cached_response_requires_exact_query_identity(tmp_path, field, value):
    body = {'request': {'code': '159915', 'start': '2026-08-28', 'end': '2026-09-28'},
            'source_url': history.REPORT_URL, 'response': []}
    path = tmp_path / '159915.json'
    path.write_text(json.dumps(body))
    assert history._load_cached_history(path, '159915', '2026-08-28', '2026-09-28', 1000) == body
    body[field] = value
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match='identity_mismatch'):
        history._load_cached_history(path, '159915', '2026-08-28', '2026-09-28', 1000)


def test_oversized_response_is_not_retried_or_parsed(monkeypatch):
    monkeypatch.setattr(history, '_require_bounded_curl', lambda: None)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        Path(command[command.index('--output') + 1]).write_bytes(b'x' * 11)
        return history.subprocess.CompletedProcess(command, 63, stdout='', stderr='')
    monkeypatch.setattr(history.subprocess, 'run', run)
    budget = RequestBudget(10, 0, max_response_bytes=10)
    with pytest.raises(RuntimeError, match='size_exceeded'):
        history._request_json(history._session(), {}, budget=budget)
    assert len(calls) == 1 and budget.request_count == 1
    assert len(budget.pending_responses) == 1


def test_cache_size_and_symlink_are_rejected(tmp_path):
    path = tmp_path / 'body.json'
    path.write_bytes(b'x' * 5000)
    with pytest.raises(ValueError, match='size_or_path'):
        history._load_cached_history(path, '159915', 'a', 'b', 10)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(ValueError, match='size_or_path'):
        history._load_cached_history(link, '159915', 'a', 'b', 10000)


@pytest.mark.parametrize('oversized', [False, True])
def test_real_curl_bounds_local_http_body_without_content_length(monkeypatch, oversized):
    """Real transport, localhost only; no exchange or control-plane mock success."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(b'x' * 4096 if oversized else b'[]')
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(history, '_report_url', lambda _: f'http://127.0.0.1:{server.server_port}/history')
    budget = RequestBudget(2, 0, max_response_bytes=64, max_total_response_bytes=128)
    try:
        if oversized:
            with pytest.raises(RuntimeError, match='size_exceeded'):
                history._request_json(history._session(), {'txtDm': '159915'}, budget=budget)
            assert len(budget.pending_responses) == 1
        else:
            assert history._request_json(history._session(), {'txtDm': '159915'}, budget=budget) == []
            assert budget.response_bytes == 2 and not budget.pending_responses
        assert len(calls) == 1 and budget.request_count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_subprocess_timeout_keeps_reservation_and_stops_at_task_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(history.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(history, '_require_bounded_curl', lambda: None)
    calls = []
    def run(command, **kwargs):
        calls.append(kwargs)
        clock[0] += kwargs['timeout']
        raise history.subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(history.subprocess, 'run', run)
    budget = RequestBudget(4, 0, max_duration_seconds=0.5)
    with pytest.raises(RuntimeError, match='deadline'):
        history._request_json(history._session(), {}, budget=budget)
    assert len(calls) == 1 and calls[0]['timeout'] <= 0.5
    assert budget.request_count == 1 and len(budget.pending_responses) == 1


def test_failed_attempt_reservation_is_persisted_before_transport(tmp_path):
    budget = RequestBudget(2, 0, max_response_bytes=100)
    budget.report_path = tmp_path / 'request-budget.json'
    attempt = budget.before_request()
    report = json.loads(budget.report_path.read_text())
    assert report['pendingAttempts'] == [attempt]
    assert report['unknownResponseBytesReserved'] == 100
    budget.finish_response(attempt, 20)
    report = json.loads(budget.report_path.read_text())
    assert report['unknownResponseBytesReserved'] == 0 and report['observedResponseBytes'] == 20


def test_old_curl_rejected_before_any_source_request(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return history.subprocess.CompletedProcess(command, 0, stdout='curl 7.88.1', stderr='')
    history._require_bounded_curl.cache_clear()
    monkeypatch.setattr(history.subprocess, 'run', run)
    budget = RequestBudget(2, 0)
    try:
        with pytest.raises(RuntimeError, match='curl_8_4_required'):
            history._request_json(history._session(), {}, budget=budget)
        assert calls == [['curl', '--version']] and budget.request_count == 0
    finally:
        history._require_bounded_curl.cache_clear()


def test_full_multidate_capture_reuses_verified_cache_without_refetch(tmp_path, monkeypatch):
    codes = {str(159000 + i): {'instrument_name': f'ETF {i}', 'etf_type': 'ETF'} for i in range(500)}
    monkeypatch.setattr(history, '_load_code_overrides', lambda _: codes)
    monkeypatch.setattr(history, '_now', lambda: '2026-11-18T00:00:00Z')
    monkeypatch.setattr(sys, 'argv', [
        'recover', '--dates', '2026-08-28', '2026-09-01', '--output-root', str(tmp_path),
        '--min-interval', '0', '--max-requests', '500', '--workers', '1'])
    calls = []
    def fetch(code, start, end, *, budget):
        calls.append((code, start, end))
        n = budget.before_request()
        payload = {'request': {'code': code, 'start': start, 'end': end},
                   'source_url': history.REPORT_URL,
                   'response': [{'data': [{'size_date': d, 'current_size': '12.5',
                       'security_short_name': 'ETF'} for d in ('2026-08-28', '2026-09-01')]}]}
        budget.finish_response(n, len(json.dumps(payload).encode()))
        return code, payload
    monkeypatch.setattr(history, '_historical_response', fetch)
    history.main()
    assert len(calls) == 500 and len(set((s, e) for _, s, e in calls)) == 1
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert manifest['requestCount'] == 500 and len(manifest['artifacts']) == 2
    assert manifest['cachedFileBytes'] == sum(path.stat().st_size for path in (tmp_path / 'responses').glob('*.json'))
    monkeypatch.setattr(history, '_historical_response', lambda *a, **k: pytest.fail('cache must avoid HTTP'))
    history.main()
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert manifest['requestCount'] == 0 and manifest['cachedFileBytes'] > 0
    monkeypatch.setattr(sys, 'argv', [*sys.argv, '--max-total-cached-bytes', '1'])
    with pytest.raises(RuntimeError, match='cache_budget_exceeded'):
        history.main()
    monkeypatch.setattr(sys, 'argv', [
        'recover', '--dates', '2026-08-28', '2026-09-01', '--output-root', str(tmp_path),
        '--min-interval', '0', '--max-requests', '500', '--workers', '1'])
    cache = tmp_path / 'responses/159000.json'
    value = json.loads(cache.read_text());value['request']['start'] = '2026-08-27';cache.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='identity_mismatch'):
        history.main()
