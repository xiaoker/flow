"""Deterministic PCF v2 canonical artifacts and raw-pack builder.

This module is offline-only.  It never runs in the Worker request path and it
does not mutate R2/D1.  A finalize job can use the returned bytes and manifest
to upload immutable objects, then switch a single history pointer after every
checksum and coverage gate passes.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 2
CALCULATION_VERSION = "pcf-capture-v2"
SOURCE_WINDOW_VERSION = "sse-pcf-exact-v1"
QUALIFICATION_VERSION = "pcf-qualification-v1"

_COLUMNS: dict[str, tuple[str, ...]] = {
    "headers": (
        "market", "etf_code", "etf_name", "fund_instrument_id",
        "trading_day", "pre_trading_day", "creation_redemption_unit",
        "record_number", "source_extra_json",
    ),
    "components": (
        "market", "etf_code", "etf_name", "stock_code", "stock_name",
        "quantity", "creation_unit", "substitution_flag", "listing_market",
        "source_extra_json",
    ),
    "data": (
        "trade_date", "pcf_date", "pcf_previous_date", "market", "etf_code",
        "etf_name", "stock_code", "stock_name", "quantity", "creation_unit",
        "substitution_flag", "listing_market", "source_id", "source_grade",
        "date_basis", "observed_at",
    ),
}

_ALIASES = {
    "fundInstrumentId": "fund_instrument_id",
    "tradingDay": "trading_day",
    "preTradingDay": "pre_trading_day",
    "creationRedemptionUnit": "creation_redemption_unit",
    "recordNumber": "record_number",
    "sourceExtraJson": "source_extra_json",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _stable_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value).decode("utf-8")
    return str(value)


def _gzip_deterministic(content: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=6) as stream:
        stream.write(content)
    return output.getvalue()


def _rows(value: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    return [{_ALIASES.get(str(key), str(key)): item_value for key, item_value in dict(item).items()} for item in (value or [])]


def _csv_gzip(name: str, rows: Iterable[Mapping[str, Any]]) -> tuple[bytes, int, str]:
    records = _rows(rows)
    base_columns = list(_COLUMNS[name])
    extra_columns = sorted({str(key) for row in records for key in row if str(key) not in base_columns})
    columns = base_columns + extra_columns
    plain = io.StringIO(newline="")
    writer = csv.writer(plain, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for row in sorted(records, key=lambda item: tuple(_stable_value(item.get(column)) for column in columns)):
        writer.writerow([_stable_value(row.get(column)) for column in columns])
    content = _gzip_deterministic(plain.getvalue().encode("utf-8"))
    return content, len(records), hashlib.sha256(content).hexdigest()


def _tar_member(name: str, content: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def build_deterministic_raw_pack(raw_xml: Mapping[str, bytes], raw_metadata: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[bytes, dict[str, Any], str]:
    """Build a byte-stable tar.gz with an index and code-sorted XML members."""

    members: list[dict[str, Any]] = []
    metadata = raw_metadata or {}
    for code in sorted(raw_xml):
        content = bytes(raw_xml[code])
        item = {"etfCode": str(code), "file": f"{code}.xml", "bytes": len(content), "rawSha256": hashlib.sha256(content).hexdigest()}
        for field in ("objectSha256", "tradingDay", "preTradingDay"):
            if metadata.get(str(code), {}).get(field) not in (None, ""):
                item[field] = metadata[str(code)][field]
        members.append(item)
    index = {"schemaVersion": SCHEMA_VERSION, "members": members}
    index_bytes = _canonical_json(index) + b"\n"
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(_tar_member("index.json", index_bytes), io.BytesIO(index_bytes))
        for entry in members:
            content = bytes(raw_xml[entry["etfCode"]])
            archive.addfile(_tar_member(entry["file"], content), io.BytesIO(content))
    packed = _gzip_deterministic(tar_buffer.getvalue())
    return packed, index, hashlib.sha256(packed).hexdigest()


def verify_deterministic_raw_pack(pack: bytes, expected: Mapping[str, Any]) -> None:
    """Verify index/member hashes without relying on local source files."""

    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(pack)), mode="r:") as archive:
        index_member = archive.extractfile("index.json")
        if index_member is None:
            raise ValueError("raw_pack_index_missing")
        index = json.loads(index_member.read().decode("utf-8"))
        expected_members = list(expected.get("members") or [])
        actual_members = list(index.get("members") or [])
        if actual_members != expected_members:
            raise ValueError("raw_pack_index_mismatch")
        for entry in actual_members:
            member = archive.extractfile(str(entry["file"]))
            if member is None:
                raise ValueError("raw_pack_member_missing")
            content = member.read()
            if hashlib.sha256(content).hexdigest() != entry["rawSha256"]:
                raise ValueError("raw_pack_member_checksum_mismatch")


def build_pcf_canonical_artifacts(
    *,
    trade_date: str,
    previous_date: str,
    job_id: str,
    plan_sha256: str,
    headers: Iterable[Mapping[str, Any]] | None,
    components: Iterable[Mapping[str, Any]] | None,
    data: Iterable[Mapping[str, Any]] | None,
    raw_xml: Mapping[str, bytes],
    raw_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    required_count: int,
    captured_count: int,
    valid_count: int,
    failed_count: int,
    unchanged_count: int = 0,
    new_baseline_missing: int = 0,
    missing_current: int = 0,
    quality: Mapping[str, int] | None = None,
    observed_at: str | None = None,
    source_window: Mapping[str, Any] | None = None,
    source_evidence: Mapping[str, Any] | None = None,
    eligibility: Mapping[str, Any] | None = None,
    qualification: Mapping[str, Any] | None = None,
    normalized_sha256s: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return immutable artifact bytes, deterministic keys and a manifest.

    The function does not return a READY manifest unless exact required
    coverage is complete.  Callers may still use the bytes for diagnostics,
    but a partial result must not update an active pointer.
    """

    if min(required_count, captured_count, valid_count, failed_count) < 0:
        raise ValueError("coverage_count_negative")
    if valid_count > captured_count or captured_count > required_count or failed_count > required_count:
        raise ValueError("coverage_count_inconsistent")
    if not raw_xml and required_count:
        raise ValueError("raw_pack_missing_for_required_job")
    observed = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    pack_bytes, pack_index, pack_sha = build_deterministic_raw_pack(raw_xml, raw_metadata)
    csv_artifacts: dict[str, tuple[bytes, int, str]] = {
        name: _csv_gzip(name, rows)
        for name, rows in (("headers", headers), ("components", components), ("data", data))
    }
    material = "|".join([pack_sha, *(csv_artifacts[name][2] for name in ("headers", "components", "data")), job_id, plan_sha256]).encode("utf-8")
    revision_id = hashlib.sha256(material).hexdigest()[:24]
    base = f"market-data/pcf-v2"
    artifact_meta: dict[str, Any] = {
        name: {"key": f"{base}/canonical/{trade_date}/{revision_id}/{name}.csv.gz", "sha256": sha, "rows": rows}
        for name, (_, rows, sha) in csv_artifacts.items()
    }
    artifact_meta["rawPack"] = {
        "key": f"{base}/raw-pack/{trade_date}/{pack_sha}.tar.gz",
        "sha256": pack_sha,
        "members": len(pack_index["members"]),
    }
    source_window_value = dict(source_window or {
        "version": SOURCE_WINDOW_VERSION,
        "provider": "sse",
        "dataset": "etf_pcf",
        "targetDate": trade_date,
        "previousDate": previous_date,
        "exactDateRule": "TradingDay == targetDate and PreTradingDay == previousDate",
        "windowClass": "official_exact",
    })
    if source_window_value.get("targetDate") != trade_date or source_window_value.get("previousDate") != previous_date:
        raise ValueError("source_window_date_mismatch")
    eligibility_value = dict(eligibility or {
        "status": "ELIGIBLE" if required_count >= 0 else "BLOCKED",
        "basis": "COMPARABLE_CHANGED exact share delta",
        "setSha256": hashlib.sha256(b"[]").hexdigest(),
        "codes": [],
    })
    qualification_value = dict(qualification or {
        "version": QUALIFICATION_VERSION,
        "status": "PENDING",
        "captureDate": trade_date,
        "qualificationDate": None,
        "eligibleSetSha256": eligibility_value.get("setSha256"),
        "evidence": {},
    })
    qualification_value.setdefault("version", QUALIFICATION_VERSION)
    qualification_value.setdefault("captureDate", trade_date)
    qualification_value.setdefault("eligibleSetSha256", eligibility_value.get("setSha256"))
    if valid_count == required_count and captured_count == required_count and failed_count == 0 and required_count >= 0 and eligibility_value.get("status") == "ELIGIBLE" and qualification_value.get("status") != "BLOCKED":
        qualification_value["status"] = "QUALIFIED"
        qualification_value["qualificationDate"] = trade_date
        qualification_value["evidence"] = {
            **dict(qualification_value.get("evidence") or {}),
            "rawPackSha256": pack_sha,
            "artifactSha256": {name: artifact_meta[name]["sha256"] for name in ("headers", "components", "data")},
            "normalizedSha256s": dict(normalized_sha256s or {}),
            "validatedCount": valid_count,
        }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "dataset": "sse_etf_pcf",
        "calculationVersion": CALCULATION_VERSION,
        "tradeDate": trade_date,
        "previousDate": previous_date,
        "jobId": job_id,
        "planSha256": plan_sha256,
        "source": {
            "publisher": "上海证券交易所",
            "urlTemplate": "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode={code}",
            "dateContract": "TradingDay == tradeDate; PreTradingDay == previousDate",
        },
        "sourceWindow": source_window_value,
        "sourceEvidence": dict(source_evidence or {}),
        "eligibility": eligibility_value,
        "qualification": qualification_value,
        "universe": {
            "required": required_count,
            "unchanged": unchanged_count,
            "newBaselineMissing": new_baseline_missing,
            "missingCurrent": missing_current,
        },
        "coverage": {
            "captured": captured_count,
            "valid": valid_count,
            "failed": failed_count,
            "ppm": int(valid_count * 1_000_000 / required_count) if required_count else 1_000_000,
        },
        "artifacts": artifact_meta,
        "quality": dict(quality or {"pass": 0, "warn": 0, "fail": 0}),
        "observedAt": observed,
    }
    manifest_bytes = _canonical_json(manifest) + b"\n"
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_key = f"{base}/manifests/{trade_date}/{revision_id}.json"
    return {
        "revisionId": revision_id,
        "manifest": manifest,
        "manifestBytes": manifest_bytes,
        "manifestSha256": manifest_sha,
        "manifestKey": manifest_key,
        "artifacts": {
            **{
                name: {"key": artifact_meta[name]["key"], "bytes": content, "sha256": sha, "rows": rows}
                for name, (content, rows, sha) in csv_artifacts.items()
            },
            "rawPack": {"key": artifact_meta["rawPack"]["key"], "bytes": pack_bytes, "sha256": pack_sha, "members": len(pack_index["members"])},
        },
        "ready": valid_count == required_count and captured_count == required_count and failed_count == 0,
    }
