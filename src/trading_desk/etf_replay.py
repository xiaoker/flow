"""Bounded, offline archive-to-candidate replay through existing ETF builders.

The enclosing pinned source artifact supplies provenance authority. A matching
hash alone is not proof that arbitrary caller bytes came from an exchange.
This module performs no network, registration, activation or filesystem I/O.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
from copy import deepcopy

import pandas as pd

from .dashboard import _build_etf_terminal_data
from .etf_flow_state import build_etf_flow_state, materialize_dashboard_snapshot
from .etf_period_dependencies import validate_official_calendar, MAX_CALENDAR_DATES
from .pipeline_application import canonical_sha256

REPLAY_CONTRACT = "flow-etf-archive-replay-v0.8.0"
SCOPED_REPLAY_CONTRACT = "flow-etf-scoped-replay-v0.8.1"
MAX_SOURCE_BYTES = 2_000_000
MAX_PARTITIONS = MAX_CALENDAR_DATES * 2


def validate_replay_scope(scope: dict) -> dict:
    from .etf_flow_state import PERIOD_ORDER
    if (not isinstance(scope, dict) or set(scope) != {'periods', 'metrics'}
            or any(not isinstance(scope[k], list) or not scope[k] for k in scope)
            or any(not isinstance(v, str) for k in scope for v in scope[k])
            or any(len(set(scope[k])) != len(scope[k]) for k in scope)
            or set(scope['periods']) - (set(PERIOD_ORDER) - {'1d'})
            or set(scope['metrics']) - {'shares', 'estimatedAmount'}):
        raise ValueError('etf_replay_scope_invalid')
    return {'periods': [p for p in PERIOD_ORDER if p in scope['periods']],
            'metrics': sorted(scope['metrics'])}


def validated_replay_inputs(profile: dict, target: str):
    """Validate exact pinned canonical bytes without materializing a view."""
    scoped = isinstance(profile, dict) and profile.get('contractVersion') == SCOPED_REPLAY_CONTRACT
    keys = {'contractVersion', 'calendar', 'partitions'} | ({'scope'} if scoped else set())
    if not isinstance(profile, dict) or set(profile) != keys or profile.get("contractVersion") not in {REPLAY_CONTRACT, SCOPED_REPLAY_CONTRACT}:
        raise ValueError("etf_replay_contract_invalid")
    if scoped:
        validate_replay_scope(profile['scope'])
    dates, _ = validate_official_calendar(profile["calendar"])
    if target not in dates:
        raise ValueError("etf_replay_target_not_in_calendar")
    dates = dates[:dates.index(target)+1]
    partitions = profile["partitions"]
    if not isinstance(partitions, list) or not partitions or len(partitions)>MAX_PARTITIONS:
        raise ValueError("etf_replay_partitions_invalid")
    frames = {}
    evidence = []
    total = 0
    for part in partitions:
        if not isinstance(part, dict) or set(part) != {"sourceId", "tradeDate", "objectKey", "sha256", "contentBase64"}:
            raise ValueError("etf_replay_partition_identity_invalid")
        source, day = part["sourceId"], part["tradeDate"]
        if not isinstance(source,str) or not isinstance(day,str) or source not in {"sse", "szse"} or day not in dates or (source,day) in frames:
            raise ValueError("etf_replay_partition_identity_invalid")
        key = part["objectKey"]
        if not isinstance(key,str) or not re.fullmatch(rf"market-data/normalized/{source}/etf_scale/{day}/[a-f0-9]{{64}}/data\.csv",key):
            raise ValueError("etf_replay_partition_identity_invalid")
        encoded = part["contentBase64"]
        if not isinstance(encoded,str) or len(encoded)>2_800_000:
            raise ValueError("etf_replay_size_exceeded")
        try:
            data = base64.b64decode(encoded,validate=True)
        except ValueError:
            raise ValueError("etf_replay_bytes_invalid") from None
        total += len(data)
        if total>MAX_SOURCE_BYTES:
            raise ValueError("etf_replay_size_exceeded")
        if hashlib.sha256(data).hexdigest()!=part["sha256"]:
            raise ValueError("etf_replay_hash_mismatch")
        frame = pd.read_csv(io.BytesIO(data),dtype={"instrument_code":str})
        if (frame.empty or len(frame)>5000 or not {"trade_date","market","instrument_code","shares"}.issubset(frame)
            or set(frame.trade_date)!={day} or set(frame.market)!={source.upper()}
            or frame.instrument_code.duplicated().any()
            or not frame.instrument_code.str.fullmatch(r"\d{6}").fillna(False).all()):
            raise ValueError("etf_replay_rows_invalid")
        counts = pd.to_numeric(frame.shares,errors="coerce")
        # Existing SSE normalized bytes retain float conversion noise from
        # 万份 -> 份 (e.g. n+0.000001). Preserve them and the existing formula;
        # do not invent a stricter integer schema or silently round inputs.
        if counts.isna().any() or (counts<0).any() or not all(math.isfinite(v) for v in counts):
            raise ValueError("etf_replay_shares_invalid")
        frames[(source,day)] = frame
        evidence.append({k:part[k] for k in ("sourceId","tradeDate","objectKey","sha256")})
    if not any(day==target for _,day in frames):
        raise ValueError("etf_replay_target_partition_missing")
    return dates, frames, evidence


def _scoped_result(snapshot: dict, rebuilt: dict, profile: dict) -> dict:
    """Patch only approved period/metric cells; never replace history metadata."""
    scope = validate_replay_scope(profile['scope'])
    base = deepcopy(snapshot)
    del base['etfReplayInput']
    if base.get('publication', {}).get('status') != 'confirmed':
        raise ValueError('etf_replay_scoped_base_not_confirmed')
    old = base.get('etfTerminal', {})
    new = rebuilt['etfTerminal']
    proof = new['windowEvidence']
    for period in scope['periods']:
        if proof.get('periods', {}).get(period, {}).get('status') != 'READY':
            raise ValueError('etf_replay_scoped_window_incomplete')

    def indexed(rows, fields):
        if not isinstance(rows, list):
            raise ValueError('etf_replay_scoped_universe_invalid')
        values = {tuple(row.get(k) for k in fields): row for row in rows if isinstance(row, dict)}
        if len(values) != len(rows):
            raise ValueError('etf_replay_scoped_universe_invalid')
        return values

    rows = indexed(old.get('instruments'), ('market', 'code'))
    replacement = indexed(new.get('instruments'), ('market', 'code'))
    groups = indexed(old.get('groupPeriods'), ('name',))
    new_groups = indexed(new.get('groupPeriods'), ('name',))
    if rows.keys() != replacement.keys() or groups.keys() != new_groups.keys():
        raise ValueError('etf_replay_scoped_universe_changed')
    for key, row in rows.items():
        if any(row.get(field) != replacement[key].get(field) for field in ('category', 'groupType', 'assetClass')):
            raise ValueError('etf_replay_scoped_classification_changed')
    for key, group in groups.items():
        if group.get('componentCount') != new_groups[key].get('componentCount'):
            raise ValueError('etf_replay_scoped_universe_changed')

    fields = {'shares': ('periods', 'periodCoverage', 'comparableCount'),
              'estimatedAmount': ('estimatedCashPeriods', 'cashPeriodCoverage', 'estimatedCashCoverage')}
    for metric in scope['metrics']:
        value_field, count_field, group_count = fields[metric]
        for targets, sources, coverage in ((rows, replacement, count_field), (groups, new_groups, group_count)):
            for key, row in targets.items():
                incoming = sources[key]
                for period in scope['periods']:
                    value = incoming.get(value_field, {}).get(period)
                    count = incoming.get(coverage, {}).get(period)
                    old_value = row.get(value_field, {}).get(period)
                    old_count = row.get(coverage, {}).get(period, 0)
                    if (not isinstance(count, int) or isinstance(count, bool)
                            or not isinstance(old_count, int) or isinstance(old_count, bool)
                            or count < old_count or (old_value is not None and value is None)):
                        raise ValueError('etf_replay_scoped_coverage_regressed')
                    row.setdefault(value_field, {})[period] = value
                    row.setdefault(coverage, {})[period] = count
    # Unselected evidence retains its original semantics. Scoped evidence may
    # prove only the explicitly repaired cells, never certify legacy windows.
    prior = old.get('windowEvidence')
    window = deepcopy(prior) if isinstance(prior, dict) else {
        'contractVersion': 'flow-etf-window-v0.8.0', 'periods': {}, 'legacyFallback': True}
    scoped_windows = window.setdefault('scopedWindows', {})
    for period in scope['periods']:
        for metric in scope['metrics']:
            scoped_windows.setdefault(period, {})[metric] = {
                **deepcopy(proof['periods'][period]), 'calendarSha256': proof['calendarSha256'],
                'datesSha256': proof['datesSha256'], 'sourceInputs': proof['sourceInputs'],
                'inputSha256': proof['inputSha256']}
    old['windowEvidence'] = window
    old['scopedReplay'] = {'contractVersion': SCOPED_REPLAY_CONTRACT, 'scope': scope,
        'baseSnapshotSha256': canonical_sha256({k:v for k,v in snapshot.items() if k != 'etfReplayInput'}),
        'sourceInputSha256': proof['inputSha256']}
    old['availablePeriods'] = list(dict.fromkeys([*old.get('availablePeriods', []), *scope['periods']]))
    return base


def replay_snapshot(snapshot: dict) -> dict:
    """Consume explicit immutable normalized inputs, not damaged rolling state."""
    profile = snapshot.get("etfReplayInput")
    dates, frames, evidence = validated_replay_inputs(profile, snapshot.get("asOf"))
    daily = []
    for i,day in enumerate(dates[1:],1):
        if not any((source,day) in frames for source in ("sse","szse")):
            continue
        selected = {}
        for source in ("sse","szse"):
            parts = [frames[(source,d)] for d in (dates[i-1],day) if (source,d) in frames]
            selected[source] = pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
        terminal = _build_etf_terminal_data(selected["sse"],selected["szse"],[dates[i-1],day])
        payload = {"asOf":day,"etfTerminal":terminal}
        content = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()
        daily.append((content,payload))
    if not daily:
        raise ValueError("etf_replay_baseline_unavailable")
    state = build_etf_flow_state(daily, canonical_etf=True)
    if profile['contractVersion'] == SCOPED_REPLAY_CONTRACT:
        # Classification is pinned by the published base, not recomputed by a
        # period-only repair. The final merge still rejects universe drift.
        identities = {(r['market'], r['code']): r
                      for r in snapshot.get('etfTerminal', {}).get('instruments', [])}
        for row in state['instruments']:
            original = identities.get((row['market'], row['code']))
            if original is not None:
                for field in ('category', 'groupType', 'assetClass'):
                    if field in original:
                        row[field] = original[field]
    state["officialCalendar"] = profile["calendar"]
    base = deepcopy(snapshot)
    del base["etfReplayInput"]
    result = materialize_dashboard_snapshot(base,state)
    proof = result["etfTerminal"]["windowEvidence"]
    proof["sourceInputs"] = sorted(evidence,key=lambda r:(r["tradeDate"],r["sourceId"]))
    proof["inputSha256"] = canonical_sha256({"contractVersion":REPLAY_CONTRACT,
        "calendarSha256":proof["calendarSha256"],"datesSha256":proof["datesSha256"],"sourceInputs":proof["sourceInputs"]})
    if profile['contractVersion'] == SCOPED_REPLAY_CONTRACT:
        return _scoped_result(snapshot, result, profile)
    return result
