"""PCF v2 contracts and deterministic ETF capture planning.

The planner and contracts are deliberately side-effect free.  XML download,
R2/D1 orchestration and finalize remain separate shadow/control-plane paths;
the production reader is not switched until the later capability gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PCF_CAPTURE_CALCULATION_VERSION = "pcf-capture-v2"
PCF_SOURCE_WINDOW_VERSION = "sse-pcf-exact-v1"
PCF_QUALIFICATION_VERSION = "pcf-qualification-v1"
PCF_PLAN_HARD_LIMIT = 2000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BaselineStatus = Literal[
    "COMPARABLE_CHANGED",
    "COMPARABLE_UNCHANGED",
    "NEW_BASELINE_MISSING",
    "MISSING_CURRENT",
    "INVALID_SHARE",
]
CaptureJobStatus = Literal[
    "PLANNED",
    "RUNNING",
    "PARTIAL",
    "RATE_LIMITED",
    "EXPIRED_EXACT_WINDOW",
    "BLOCKED_QUALITY",
    "BLOCKED_UNIVERSE_ANOMALY",
    "READY_NO_CHANGE",
    "FINALIZING",
    "FINALIZE_FAILED",
    "READY",
]
CaptureItemState = Literal[
    "NOT_REQUIRED",
    "PENDING",
    "RUNNING",
    "RETRYABLE",
    "CAPTURED",
    "VALID",
    "BLOCKED",
]


def _iso_date(value: Any, *, field_name: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not ISO_DATE_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a calendar date") from exc
    return text


def _sha256(value: str, *, field_name: str) -> str:
    text = str(value).lower()
    if not SHA256_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return text


def _code(value: Any) -> str:
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text.upper()


def _share(value: Any, *, field_name: str) -> int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    nearest = number.to_integral_value()
    # SSE publishes scale in 10k shares.  The normalized CSV historically
    # passed through binary floats, so values such as 7633566800.000001 are
    # serialization noise rather than fractional shares.  Accept only a tiny
    # sub-share tolerance; material fractions remain a hard quality failure.
    if abs(number - nearest) > Decimal("0.001"):
        raise ValueError(f"{field_name} must be a non-negative integer")
    return int(nearest)


class PcfCaptureItemContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    job_id: str = Field(alias="jobId")
    etf_code: str = Field(alias="etfCode")
    etf_name: str = Field(alias="etfName")
    current_shares: int | None = Field(default=None, alias="currentShares")
    previous_shares: int | None = Field(default=None, alias="previousShares")
    share_delta: int | None = Field(default=None, alias="shareDelta")
    baseline_status: BaselineStatus = Field(alias="baselineStatus")
    state: CaptureItemState
    attempts: int = 0
    next_attempt_at: str | None = Field(default=None, alias="nextAttemptAt")
    http_status: int | None = Field(default=None, alias="httpStatus")
    source_trading_day: str | None = Field(default=None, alias="sourceTradingDay")
    source_previous_day: str | None = Field(default=None, alias="sourcePreviousDay")
    raw_object_key: str | None = Field(default=None, alias="rawObjectKey")
    raw_object_sha256: str | None = Field(default=None, alias="rawObjectSha256")
    raw_content_sha256: str | None = Field(default=None, alias="rawContentSha256")
    raw_bytes: int | None = Field(default=None, alias="rawBytes")
    component_count: int | None = Field(default=None, alias="componentCount")
    normalized_sha256: str | None = Field(default=None, alias="normalizedSha256")
    failure_reason: str | None = Field(default=None, alias="failureReason")
    observed_at: str | None = Field(default=None, alias="observedAt")
    updated_at: str = Field(alias="updatedAt")

    @field_validator("etf_code", mode="before")
    @classmethod
    def normalize_code(cls, value: Any) -> str:
        code = _code(value)
        if not code:
            raise ValueError("etfCode is required")
        return code

    @field_validator("current_shares", "previous_shares", "share_delta", mode="before")
    @classmethod
    def validate_share_fields(cls, value: Any, info: Any) -> int | None:
        if value is None:
            return None
        if info.field_name == "share_delta":
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("shareDelta must be an integer") from exc
            if not math.isfinite(number) or not number.is_integer():
                raise ValueError("shareDelta must be an integer")
            return int(number)
        return _share(value, field_name=info.field_name)

    @field_validator("raw_object_sha256", "raw_content_sha256", "normalized_sha256", mode="before")
    @classmethod
    def validate_optional_hash(cls, value: Any, info: Any) -> str | None:
        return None if value in (None, "") else _sha256(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_status(self) -> "PcfCaptureItemContract":
        if self.baseline_status == "COMPARABLE_CHANGED" and self.share_delta == 0:
            raise ValueError("changed baseline cannot have zero share delta")
        if self.baseline_status == "COMPARABLE_UNCHANGED" and self.share_delta != 0:
            raise ValueError("unchanged baseline must have zero share delta")
        if self.state == "NOT_REQUIRED" and self.baseline_status == "COMPARABLE_CHANGED":
            raise ValueError("changed ETF must remain required")
        return self


class PcfCaptureJobContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    job_id: str = Field(alias="jobId")
    target_date: str = Field(alias="targetDate")
    previous_date: str = Field(alias="previousDate")
    plan_sha256: str = Field(alias="planSha256")
    calculation_version: str = Field(alias="calculationVersion")
    status: CaptureJobStatus
    expected_count: int = Field(alias="expectedCount", ge=0)
    required_count: int = Field(alias="requiredCount", ge=0)
    unchanged_count: int = Field(alias="unchangedCount", ge=0)
    new_baseline_count: int = Field(alias="newBaselineCount", ge=0)
    missing_current_count: int = Field(alias="missingCurrentCount", ge=0)
    captured_count: int = Field(default=0, alias="capturedCount", ge=0)
    valid_count: int = Field(default=0, alias="validCount", ge=0)
    failed_count: int = Field(default=0, alias="failedCount", ge=0)
    coverage_ppm: int = Field(default=0, alias="coveragePpm", ge=0, le=1_000_000)
    lease_owner: str | None = Field(default=None, alias="leaseOwner")
    lease_expires_at: str | None = Field(default=None, alias="leaseExpiresAt")
    next_attempt_at: str | None = Field(default=None, alias="nextAttemptAt")
    canonical_manifest_key: str | None = Field(default=None, alias="canonicalManifestKey")
    canonical_manifest_sha256: str | None = Field(default=None, alias="canonicalManifestSha256")
    raw_pack_key: str | None = Field(default=None, alias="rawPackKey")
    raw_pack_sha256: str | None = Field(default=None, alias="rawPackSha256")
    error_json: dict[str, Any] = Field(default_factory=dict, alias="errorJson")
    source_window: dict[str, Any] = Field(default_factory=dict, alias="sourceWindow")
    eligibility: dict[str, Any] = Field(default_factory=dict)
    qualification: dict[str, Any] = Field(default_factory=dict)
    started_at: str = Field(alias="startedAt")
    completed_at: str | None = Field(default=None, alias="completedAt")
    updated_at: str = Field(alias="updatedAt")

    @field_validator("target_date", "previous_date", mode="before")
    @classmethod
    def validate_dates(cls, value: Any, info: Any) -> str:
        return _iso_date(value, field_name=info.field_name)

    @field_validator("plan_sha256", "canonical_manifest_sha256", "raw_pack_sha256", mode="before")
    @classmethod
    def validate_hashes(cls, value: Any, info: Any) -> str | None:
        return None if value in (None, "") else _sha256(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_counts(self) -> "PcfCaptureJobContract":
        if self.required_count > self.expected_count:
            raise ValueError("requiredCount cannot exceed expectedCount")
        if self.captured_count > self.required_count or self.valid_count > self.captured_count:
            raise ValueError("capture counts are not monotonic")
        if self.status in {"READY", "READY_NO_CHANGE"}:
            if self.failed_count or self.valid_count != self.required_count:
                raise ValueError("ready job requires complete valid coverage")
            qualification = self.qualification
            if not qualification or qualification.get("status") not in {"QUALIFIED", "READY"}:
                raise ValueError("ready job requires qualified PCF evidence")
        return self


class PcfArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    sha256: str
    rows: int = Field(default=0, ge=0)
    members: int | None = Field(default=None, ge=0)

    @field_validator("sha256", mode="before")
    @classmethod
    def validate_hash(cls, value: Any) -> str:
        return _sha256(value, field_name="sha256")


class PcfManifestContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
    dataset: Literal["sse_etf_pcf"]
    calculation_version: Literal["pcf-capture-v2"] = Field(alias="calculationVersion")
    trade_date: str = Field(alias="tradeDate")
    previous_date: str = Field(alias="previousDate")
    job_id: str = Field(alias="jobId")
    plan_sha256: str = Field(alias="planSha256")
    source: dict[str, Any] = Field(default_factory=dict)
    universe: dict[str, int]
    coverage: dict[str, int]
    artifacts: dict[str, PcfArtifactContract]
    quality: dict[str, int]
    observed_at: str = Field(alias="observedAt")
    source_window: dict[str, Any] = Field(default_factory=dict, alias="sourceWindow")
    source_evidence: dict[str, Any] = Field(default_factory=dict, alias="sourceEvidence")
    eligibility: dict[str, Any] = Field(default_factory=dict)
    qualification: dict[str, Any] = Field(default_factory=dict)

    @field_validator("trade_date", "previous_date", mode="before")
    @classmethod
    def validate_manifest_dates(cls, value: Any, info: Any) -> str:
        return _iso_date(value, field_name=info.field_name)

    @field_validator("plan_sha256", mode="before")
    @classmethod
    def validate_manifest_hash(cls, value: Any) -> str:
        return _sha256(value, field_name="planSha256")

    @model_validator(mode="after")
    def validate_coverage(self) -> "PcfManifestContract":
        required = self.universe.get("required")
        captured = self.coverage.get("captured")
        valid = self.coverage.get("valid")
        failed = self.coverage.get("failed")
        if not all(isinstance(value, int) and value >= 0 for value in (required, captured, valid, failed)):
            raise ValueError("manifest coverage counters must be non-negative integers")
        if valid > captured or captured > required or failed > required:
            raise ValueError("manifest coverage counters are inconsistent")
        qualification = self.qualification
        if qualification:
            if qualification.get("version") not in {None, PCF_QUALIFICATION_VERSION}:
                raise ValueError("manifest qualification version is unsupported")
            if qualification.get("status") == "QUALIFIED":
                if qualification.get("captureDate") != self.trade_date or qualification.get("qualificationDate") != self.trade_date:
                    raise ValueError("qualified manifest dates must match tradeDate")
        return self


class HistoryArtifactPointerContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_id: str = Field(alias="sourceId")
    dataset: str
    trade_date: str = Field(alias="tradeDate")
    file_name: str = Field(alias="fileName")
    revision_id: str = Field(alias="revisionId")
    object_key: str = Field(alias="objectKey")
    sha256: str
    content_encoding: str | None = Field(default=None, alias="contentEncoding")
    row_count: int = Field(alias="rowCount", ge=0)
    observed_at: str = Field(alias="observedAt")
    status: str
    updated_at: str = Field(alias="updatedAt")

    @field_validator("trade_date", mode="before")
    @classmethod
    def validate_pointer_date(cls, value: Any) -> str:
        return _iso_date(value, field_name="tradeDate")

    @field_validator("sha256", mode="before")
    @classmethod
    def validate_pointer_hash(cls, value: Any) -> str:
        return _sha256(value, field_name="sha256")


def _records(value: Iterable[Mapping[str, Any]] | pd.DataFrame | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    return [dict(item) for item in value]


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def eligible_set_sha256(items: Iterable[Mapping[str, Any]]) -> str:
    """Hash the exact, sorted ETF set eligible for PCF capture.

    The set hash is deliberately independent of ordering, observation time and
    share values.  It is the join key between planning, checkpointing and
    qualification evidence.
    """

    codes = sorted(
        f"{str(item.get('market') or '').upper()}:{_code(item.get('etfCode', item.get('etf_code', item.get('code'))))}"
        for item in items
        if bool(item.get("required"))
    )
    return _canonical_hash(codes)


def default_source_window(target_date: str, previous_date: str, *, probe_code: str = "plan-input") -> dict[str, Any]:
    return {
        "version": PCF_SOURCE_WINDOW_VERSION,
        "provider": "sse",
        "dataset": "etf_pcf",
        "probeCode": probe_code,
        "targetDate": target_date,
        "previousDate": previous_date,
        "exactDateRule": "TradingDay == targetDate and PreTradingDay == previousDate",
        "windowClass": "official_exact",
    }


def pending_qualification(target_date: str, eligible_sha256: str) -> dict[str, Any]:
    return {
        "version": PCF_QUALIFICATION_VERSION,
        "status": "PENDING",
        "captureDate": target_date,
        "qualificationDate": None,
        "eligibleSetSha256": eligible_sha256,
        "evidence": {},
    }


def _normalise_universe(
    value: Iterable[Mapping[str, Any]] | pd.DataFrame | None,
    *,
    source_name: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in _records(value):
        code = _code(raw.get("instrument_code", raw.get("etf_code", raw.get("code"))))
        market = str(raw.get("market", "")).strip().upper()
        if not code or not market:
            raise ValueError(f"{source_name}_key_missing")
        key = (market, code)
        if key in result:
            raise ValueError(f"{source_name}_duplicate:{market}:{code}")
        try:
            shares = _share(raw.get("shares"), field_name=f"{source_name}.shares")
            share_error = False
        except ValueError:
            shares = None
            share_error = True
        result[key] = {
            "market": market,
            "instrument_code": code,
            "instrument_name": str(raw.get("instrument_name", raw.get("name", code))).strip() or code,
            "shares": shares,
            "share_error": share_error,
        }
    return result


def build_changed_etf_capture_plan(
    current: Iterable[Mapping[str, Any]] | pd.DataFrame,
    previous: Iterable[Mapping[str, Any]] | pd.DataFrame,
    *,
    target_date: str,
    previous_date: str,
    calculation_version: str = PCF_CAPTURE_CALCULATION_VERSION,
    hard_limit: int = PCF_PLAN_HARD_LIMIT,
    source_window: Mapping[str, Any] | None = None,
    source_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic outer-join PCF capture plan.

    Only ``COMPARABLE_CHANGED`` rows are required PCF requests.  Missing
    baselines and current rows remain explicit in the plan so they can block
    a later capability gate instead of disappearing through an inner join.
    No sampling or truncation is performed when the hard limit is exceeded.
    """

    target_date = _iso_date(target_date, field_name="targetDate")
    previous_date = _iso_date(previous_date, field_name="previousDate")
    if previous_date >= target_date:
        raise ValueError("previousDate must be earlier than targetDate")
    if hard_limit <= 0:
        raise ValueError("hard_limit must be positive")

    current_map = _normalise_universe(current, source_name="current")
    previous_map = _normalise_universe(previous, source_name="previous")
    keys = sorted(set(current_map) | set(previous_map))
    items: list[dict[str, Any]] = []
    for key in keys:
        current_row = current_map.get(key)
        previous_row = previous_map.get(key)
        current_shares = current_row["shares"] if current_row else None
        previous_shares = previous_row["shares"] if previous_row else None
        name = (current_row or previous_row)["instrument_name"]
        if (current_row and current_row["share_error"]) or (previous_row and previous_row["share_error"]):
            status: BaselineStatus = "INVALID_SHARE"
        elif current_row is None:
            status = "MISSING_CURRENT"
        elif previous_row is None:
            status = "NEW_BASELINE_MISSING"
        elif current_shares != previous_shares:
            status = "COMPARABLE_CHANGED"
        else:
            status = "COMPARABLE_UNCHANGED"
        delta = None if current_shares is None or previous_shares is None else current_shares - previous_shares
        item_state: CaptureItemState = "PENDING" if status == "COMPARABLE_CHANGED" else "NOT_REQUIRED"
        items.append(
            {
                "market": key[0],
                "etfCode": key[1],
                "etfName": name,
                "currentShares": current_shares,
                "previousShares": previous_shares,
                "shareDelta": delta,
                "baselineStatus": status,
                "required": status == "COMPARABLE_CHANGED",
                "state": item_state,
            }
        )

    # The request order is deterministic and prioritises the largest absolute
    # share changes, while code order breaks ties across exchanges.
    items.sort(key=lambda item: (-abs(item["shareDelta"] or 0), item["market"], item["etfCode"]))
    required_items = [item for item in items if item["required"]]
    invalid_count = sum(item["baselineStatus"] == "INVALID_SHARE" for item in items)
    missing_current_count = sum(item["baselineStatus"] == "MISSING_CURRENT" for item in items)
    if len(required_items) > hard_limit:
        status = "BLOCKED_UNIVERSE_ANOMALY"
    elif invalid_count or missing_current_count:
        status = "BLOCKED_QUALITY"
    else:
        status = "READY_NO_CHANGE" if not required_items else "PLANNED"
    eligible_sha256 = eligible_set_sha256(required_items)
    source_window_value = dict(source_window or default_source_window(target_date, previous_date))
    if source_window_value.get("targetDate") != target_date or source_window_value.get("previousDate") != previous_date:
        raise ValueError("source_window_date_mismatch")
    eligibility_status = "ELIGIBLE" if status not in {"BLOCKED_QUALITY", "BLOCKED_UNIVERSE_ANOMALY"} else "BLOCKED"
    eligibility = {
        "status": eligibility_status,
        "basis": "COMPARABLE_CHANGED exact share delta",
        "setSha256": eligible_sha256,
        "codes": [item["etfCode"] for item in required_items],
    }
    plan_payload = {
        "targetDate": target_date,
        "previousDate": previous_date,
        "calculationVersion": calculation_version,
        "hardLimit": hard_limit,
        "status": status,
        "sourceWindow": source_window_value,
        "sourceEvidence": dict(source_evidence or {}),
        "eligibility": eligibility,
        "qualification": pending_qualification(target_date, eligible_sha256),
        "items": items,
    }
    canonical = json.dumps(plan_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    plan_sha256 = hashlib.sha256(canonical).hexdigest()
    return {
        **plan_payload,
        "planSha256": plan_sha256,
        "counts": {
            "expected": len(items),
            "required": len(required_items),
            "unchanged": sum(item["baselineStatus"] == "COMPARABLE_UNCHANGED" for item in items),
            "newBaselineMissing": sum(item["baselineStatus"] == "NEW_BASELINE_MISSING" for item in items),
            "missingCurrent": sum(item["baselineStatus"] == "MISSING_CURRENT" for item in items),
            "invalidShare": invalid_count,
        },
    }
