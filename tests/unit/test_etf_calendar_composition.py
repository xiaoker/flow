import base64
import copy
import hashlib
import json

import pytest

from trading_desk.etf_period_dependencies import (
    OFFICIAL_MARGIN_DATES_SOURCE, compose_official_margin_calendars,
    validate_official_calendar,
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def proof(days):
    raw = json.dumps({'result': [{'opDate': day.replace('-', '')} for day in days],
                      'pageHelp': {'pageCount': 1, 'total': len(days)}}).encode()
    return {'sourceId': OFFICIAL_MARGIN_DATES_SOURCE, 'sha256': hashlib.sha256(raw).hexdigest(),
            'dates': days, 'datesSha256': digest(days), 'evidence': {
                'rangeStart': days[0], 'rangeEnd': days[-1], 'contentBase64': base64.b64encode(raw).decode()}}


def parts():
    return [proof(['2026-10-15', '2026-10-16', '2026-10-19', '2026-10-20']),
            proof(['2026-10-20', '2026-10-21', '2026-10-22'])]


def test_composition_preserves_original_proofs_and_is_order_independent():
    values = parts()
    original = copy.deepcopy(values)
    result = compose_official_margin_calendars(values)
    assert result == compose_official_margin_calendars(reversed(values))
    assert result['evidence']['calendars'] == original
    assert validate_official_calendar(result)[0] == ['2026-10-15', '2026-10-16', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22']
    values[0]['dates'].clear()
    assert result['evidence']['calendars'] == original


@pytest.mark.parametrize('fault', ['hash', 'dates', 'duplicate', 'gap', 'overlap', 'nested', 'pagination'])
def test_rejects_false_or_incomplete_calendar_composition(fault):
    values = parts()
    if fault == 'hash': values[0]['sha256'] = '0' * 64
    elif fault == 'dates': values[0]['dates'].pop()
    elif fault == 'duplicate': values.append(copy.deepcopy(values[0]))
    elif fault == 'gap': values[1] = proof(['2026-10-21', '2026-10-22'])
    elif fault == 'overlap': values[1] = proof(['2026-10-19', '2026-10-21', '2026-10-22'])
    elif fault == 'nested': values[0] = compose_official_margin_calendars(values)
    else:
        raw = json.loads(base64.b64decode(values[0]['evidence']['contentBase64']))
        raw['pageHelp']['pageCount'] = 2
        encoded = json.dumps(raw).encode()
        values[0]['evidence']['contentBase64'] = base64.b64encode(encoded).decode()
        values[0]['sha256'] = hashlib.sha256(encoded).hexdigest()
    with pytest.raises(ValueError):
        compose_official_margin_calendars(values)


def test_outer_hash_and_union_are_verified():
    result = compose_official_margin_calendars(parts())
    result['sha256'] = '0' * 64
    with pytest.raises(ValueError): validate_official_calendar(result)
    result = compose_official_margin_calendars(parts())
    result['dates'].pop()
    result['datesSha256'] = digest(result['dates'])
    with pytest.raises(ValueError): validate_official_calendar(result)


def test_infinite_input_is_bounded_before_materializing():
    observed = []
    def infinite():
        while True:
            observed.append(1)
            yield parts()[0]
    with pytest.raises(ValueError, match='limit_exceeded'):
        compose_official_margin_calendars(infinite())
    assert len(observed) == 17


def test_planner_uses_exact_six_sessions_and_does_not_compress_missing_market():
    from trading_desk.etf_period_dependencies import build_etf_period_rebuild_plan
    calendar = compose_official_margin_calendars(parts())
    sources = []
    for day in calendar['dates']:
        for market in ('SSE', 'SZSE'):
            raw_hash = digest([day, market, 'raw'])
            sources.append({'tradeDate': day, 'market': market,
                'raw': {'status': 'AVAILABLE', 'sha256': raw_hash, 'bytes': 128},
                'normalized': {'status': 'AVAILABLE', 'sha256': digest([day, market]),
                    'sourceSha256': raw_hash, 'bytes': 128, 'rowCount': 1,
                    'shareRows': 1, 'navAlignedRows': 1}})
    def plan(rows):
        return build_etf_period_rebuild_plan(calendar=calendar, target_dates=['2026-10-22'],
            source_evidence=rows, existing_models=[], periods=('1w',))
    result = plan(sources)
    assert result['windows'][0]['baselineDate'] == '2026-10-15'
    assert result['windows'][0]['dailyChangeDates'] == calendar['dates'][1:]
    missing = [row for row in sources if (row['tradeDate'], row['market']) != ('2026-10-16', 'SZSE')]
    incomplete = plan(missing)
    assert incomplete['windows'][0]['baselineDate'] == '2026-10-15'
    assert '2026-10-16' in incomplete['windows'][0]['snapshotDates']
    assert incomplete != result


def test_composed_calendar_replays_one_week_through_real_candidate_builder():
    from test_etf_replay import scoped_source
    from trading_desk.etf_replay import replay_snapshot
    from trading_desk.read_models import build_market_read_models
    from trading_desk.read_model_capabilities import CAPABILITY_CONTRACT_VERSION
    value = scoped_source()
    calendar = compose_official_margin_calendars(parts())
    value['asOf'] = '2026-10-22'
    value['etfTerminal']['historyCoverage']['to'] = '2026-10-22'
    for source_status in value['dataStatus']:
        source_status['asOf'] = '2026-10-22'
    profile = value['etfReplayInput']
    profile['calendar'] = calendar
    profile['scope'] = {'periods': ['1w'], 'metrics': ['shares']}
    partitions = []
    for market, code in [('sse', '510300'), ('szse', '159915')]:
        for index, day in enumerate(calendar['dates']):
            data = f'trade_date,market,instrument_code,instrument_name,shares\n{day},{market.upper()},{code},ETF,{100000000+index*1000000}\n'.encode()
            partitions.append({'sourceId': market, 'tradeDate': day,
                'objectKey': f'market-data/normalized/{market}/etf_scale/{day}/{"a"*64}/data.csv',
                'sha256': hashlib.sha256(data).hexdigest(), 'contentBase64': base64.b64encode(data).decode()})
    profile['partitions'] = partitions
    result = replay_snapshot(value)
    assert [row['periods']['1w'] for row in result['etfTerminal']['instruments']] == [.05, .05]
    _, manifest = build_market_read_models(value, require_materialized=True,
        capability_contract_version=CAPABILITY_CONTRACT_VERSION)
    assert manifest['capabilityContract']['capabilities']['etf_periods']['periods']['1w']['shares']['status'] == 'READY'
