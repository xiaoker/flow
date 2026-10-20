"""Durable source-readiness state for the daily collection state machine.

The exchange endpoints do not publish on one shared clock.  A collection run
therefore needs a small control-plane record which is independent from the
large raw/read-model payloads.  This module turns one phase-0 result into a
deterministic, serialisable ledger.  It deliberately does not decide whether
an incomplete source should be used: ``pending`` is an explicit state and the
release builder remains fail-closed.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo


READINESS_SCHEMA_VERSION = 1
ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_RECHECK_MINUTES = 30

# PR 0 contract only.  The existing source-readiness ledger continues to emit
# its legacy source states until the later capability-cutover PR.
PcfCapabilityStatus = Literal[
    "READY",
    "READY_NO_CHANGE",
    "PENDING_PLAN",
    "CAPTURING",
    "RATE_LIMITED",
    "EXPIRED_EXACT_WINDOW",
    "BLOCKED_DATE_MISMATCH",
    "BLOCKED_QUALITY",
    "FINALIZE_FAILED",
]
PCF_CAPABILITY_STATUSES: tuple[PcfCapabilityStatus, ...] = (
    "READY",
    "READY_NO_CHANGE",
    "PENDING_PLAN",
    "CAPTURING",
    "RATE_LIMITED",
    "EXPIRED_EXACT_WINDOW",
    "BLOCKED_DATE_MISMATCH",
    "BLOCKED_QUALITY",
    "FINALIZE_FAILED",
)

# Dataset names are stable control-plane identities.  They are intentionally
# not just publisher names because SSE ETF, SSE margin and SSE NAV have
# different publication contracts.
SOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "sse_etf": {"label": "上交所 ETF", "required": True},
    "szse_etf": {"label": "深交所 ETF", "required": True},
    "sse_margin": {"label": "上交所融资融券", "required": True},
    "szse_margin": {"label": "深交所融资融券", "required": True},
    "bse_margin": {"label": "北交所融资融券", "required": True},
    "stock_connect_summary": {"label": "港交所互联互通", "required": True},
    # NAV and PCF are optional enrichments.  Their readiness is still
    # recorded so a capability can be explained instead of silently showing 0.
    "sse_etf_nav": {"label": "上交所 ETF 单位净值", "required": False},
    "sse_etf_pcf": {"label": "上交所 ETF PCF", "required": False},
}


def _parse_instant(value: Any, *, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            if fallback is None:
                raise ValueError("readiness_timestamp_invalid")
            parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) != 10:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _deadline_for(target_date: str) -> datetime:
    """Return a stable local deadline, not ``now + N``.

    A stable deadline lets every retry describe the same operational promise
    and prevents a failed run from extending the deadline indefinitely.
    """

    parsed = datetime.strptime(target_date, "%Y-%m-%d").replace(
        tzinfo=ASIA_SHANGHAI,
        hour=11,
        minute=30,
        second=0,
        microsecond=0,
    )
    return parsed.astimezone(timezone.utc)


def _source_error(quality: dict[str, Any], dataset: str) -> str | None:
    errors = quality.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            step = str(item.get("step", ""))
            if step == dataset or step.startswith(f"{dataset}:"):
                message = str(item.get("message") or item.get("error_type") or "source_error")
                return message[:240]
    findings = quality.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if not isinstance(item, dict) or item.get("dataset") != dataset:
                continue
            if item.get("status") == "fail" and item.get("check_id") != "timeliness.requested_date_coverage":
                return str(item.get("message") or item.get("check_id") or "quality_failure")[:240]
    return None


def _observed_dates(quality: dict[str, Any], dataset: str) -> list[str]:
    profiles = quality.get("profiles")
    if not isinstance(profiles, dict):
        return []
    profile = profiles.get(dataset)
    if not isinstance(profile, dict):
        return []
    values = profile.get("distinct_trade_dates")
    if not isinstance(values, list):
        return []
    return sorted({date for date in (_iso_date(item) for item in values) if date})


def _missing_target(quality: dict[str, Any], dataset: str, target_date: str) -> bool:
    findings = quality.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if not isinstance(item, dict):
                continue
            if (
                item.get("dataset") == dataset
                and item.get("check_id") == "timeliness.requested_date_coverage"
                and item.get("status") == "fail"
            ):
                evidence = item.get("evidence")
                if isinstance(evidence, dict) and target_date in (evidence.get("missing_dates") or []):
                    return True
    return target_date not in _observed_dates(quality, dataset)


def _watermark(manifest: dict[str, Any], quality: dict[str, Any], dataset: str) -> dict[str, Any]:
    observed = _observed_dates(quality, dataset)
    result: dict[str, Any] = {"observedTradeDate": observed[-1] if observed else None}
    if dataset == "szse_etf":
        result.update(
            {
                "pageDate": _iso_date(manifest.get("szse_etf_page_date")),
                "effectiveTradeDate": _iso_date(manifest.get("szse_etf_trade_date")),
            }
        )
    return result


def build_source_readiness(
    manifest: dict[str, Any],
    quality: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
    now: datetime | None = None,
    recheck_minutes: int = DEFAULT_RECHECK_MINUTES,
) -> dict[str, Any]:
    """Build one idempotent readiness ledger from a phase-0 result.

    ``ready`` means the required target date is present and the source has no
    source-local error.  ``pending`` means the source answered successfully
    but its business-date watermark has not advanced.  Transport/parser/data
    contract errors are ``error`` and must never be retried as timing noise.
    """

    requested = sorted(
        date for date in (_iso_date(item) for item in manifest.get("requested_dates", [])) if date
    )
    if not requested:
        raise ValueError("readiness_requested_dates_missing")
    target_date = requested[-1]
    generated_at = _parse_instant(
        manifest.get("finished_at") or quality.get("generated_at"),
        fallback=now or datetime.now(timezone.utc),
    )
    observed_at = (now or generated_at).astimezone(timezone.utc)
    deadline = _deadline_for(target_date)
    previous_sources = {
        str(item.get("sourceId")): item
        for item in (previous or {}).get("sources", [])
        if isinstance(item, dict) and item.get("sourceId")
    }
    source_rows: list[dict[str, Any]] = []
    for dataset, definition in SOURCE_DEFINITIONS.items():
        required = bool(definition["required"])
        error = _source_error(quality, dataset)
        missing = _missing_target(quality, dataset, target_date)
        closure = ((quality.get("profiles") or {}).get(dataset) or {}).get("official_closed_dates") or {}
        closed_sha = closure.get(target_date) if dataset == "stock_connect_summary" and isinstance(closure, dict) else None
        if error:
            status = "error"
            reason = error
        elif isinstance(closed_sha, str) and len(closed_sha) == 64 and all(c in "0123456789abcdef" for c in closed_sha):
            status = "ready"
            reason = "official_source_target_date_closed"
        elif missing:
            status = "pending"
            reason = "official_source_business_date_not_ready"
        else:
            status = "ready"
            reason = "official_source_target_date_observed"

        previous_row = previous_sources.get(dataset) or {}
        same_run = previous_row.get("runId") == manifest.get("run_id")
        previous_attempt = int(previous_row.get("attempt") or 0)
        attempt = previous_attempt if same_run else previous_attempt + 1
        if attempt <= 0:
            attempt = 1
        next_check = None
        if status == "pending":
            next_check = _iso_utc(observed_at + timedelta(minutes=max(5, recheck_minutes)))
        source_rows.append(
            {
                "sourceId": dataset,
                "label": definition["label"],
                "required": required,
                "status": status,
                "attempt": attempt,
                "observedAt": _iso_utc(observed_at),
                "nextCheckAt": next_check,
                "deadlineAt": _iso_utc(deadline),
                "deadlineExceeded": observed_at > deadline,
                "observedDates": _observed_dates(quality, dataset)[-3:],
                "watermark": _watermark(manifest, quality, dataset),
                "reason": reason,
                "runId": str(manifest.get("run_id") or ""),
                "evidence": {
                    "qualityPath": str(manifest.get("quality_summary_path") or ""),
                    "combinedPath": (manifest.get("combined_paths") or {}).get(dataset),
                    **({"closureRawSha256": closed_sha} if reason == "official_source_target_date_closed" else {}),
                },
            }
        )

    required_rows = [row for row in source_rows if row["required"]]
    if any(row["status"] == "error" for row in required_rows):
        state = "error"
    elif any(row["status"] == "pending" for row in required_rows):
        state = "pending"
    else:
        state = "ready"
    pending = [row["label"] for row in required_rows if row["status"] == "pending"]
    errors = [row["label"] for row in required_rows if row["status"] == "error"]
    return {
        "schemaVersion": READINESS_SCHEMA_VERSION,
        "targetDate": target_date,
        "requestedDates": requested,
        "runId": str(manifest.get("run_id") or ""),
        "generatedAt": _iso_utc(generated_at),
        "observedAt": _iso_utc(observed_at),
        "deadlineAt": _iso_utc(deadline),
        "state": state,
        "pendingDomains": pending,
        "errorDomains": errors,
        "nextCheckAt": min(
            (row["nextCheckAt"] for row in required_rows if row["nextCheckAt"]),
            default=None,
        ),
        "sources": source_rows,
    }
