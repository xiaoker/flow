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
MAX_SOURCE_BYTES = 2_000_000
MAX_PARTITIONS = MAX_CALENDAR_DATES * 2


def replay_snapshot(snapshot: dict) -> dict:
    """Consume explicit immutable normalized inputs, not damaged rolling state."""
    profile = snapshot.get("etfReplayInput")
    if not isinstance(profile, dict) or set(profile) != {"contractVersion", "calendar", "partitions"} or profile.get("contractVersion") != REPLAY_CONTRACT:
        raise ValueError("etf_replay_contract_invalid")
    dates, _ = validate_official_calendar(profile["calendar"])
    target = snapshot.get("asOf")
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
    state["officialCalendar"] = profile["calendar"]
    base = deepcopy(snapshot)
    del base["etfReplayInput"]
    result = materialize_dashboard_snapshot(base,state)
    proof = result["etfTerminal"]["windowEvidence"]
    proof["sourceInputs"] = sorted(evidence,key=lambda r:(r["tradeDate"],r["sourceId"]))
    proof["inputSha256"] = canonical_sha256({"contractVersion":REPLAY_CONTRACT,
        "calendarSha256":proof["calendarSha256"],"datesSha256":proof["datesSha256"],"sourceInputs":proof["sourceInputs"]})
    return result
