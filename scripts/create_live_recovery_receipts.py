"""Build completion receipts for the stages actually produced by publish.

The script is intentionally read-only with respect to Cloudflare.  It binds
each receipt to the approved plan, exact data date, measured source evidence,
read-model manifest and QA files, then writes canonical bytes plus their R2
key/hash metadata below the isolated recovery output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_desk.recovery_execution import (
    assert_recovery_output_path,
    receipt_bytes,
    receipt_key,
    validate_recovery_plan,
    validate_recovery_receipt,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object required: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_release(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    value = _read(path)
    status = value.get("status")
    # The activation endpoint returns the candidate releaseId even when an
    # older candidate is deliberately left STAGED.  In that case it is not
    # the active-after pointer; use an explicit activeReleaseId/active object
    # when the endpoint supplies one, otherwise let the caller fall back to
    # the verified active-before pointer.
    if status in {"staged_older_than_active", "staged_stale_revision"}:
        for key in ("activeReleaseId", "active_release_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        active = value.get("active")
        if isinstance(active, dict):
            candidate = active.get("releaseId") or active.get("release_id")
            if isinstance(candidate, str) and candidate:
                return candidate
        return None
    for key in ("releaseId", "activeReleaseId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    active = value.get("active")
    if isinstance(active, dict):
        candidate = active.get("releaseId")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _manifest_for_stage(models_dir: Path, stage: str) -> tuple[Path, str]:
    if stage in {"etf_penetration", "stock_flow"}:
        path, slot = models_dir / "read-models-penetration" / "manifest.json", "etf_penetration"
    elif stage in {"dashboard", "core_collection", "publish"}:
        penetration = models_dir / "read-models-penetration" / "manifest.json"
        if stage == "publish" and penetration.is_file():
            path, slot = penetration, "etf_penetration"
        else:
            path, slot = models_dir / "read-models-dashboard" / "manifest.json", "dashboard"
    else:
        raise ValueError(f"live_recovery_receipt_unsupported_stage:{stage}")
    if not path.is_file():
        raise ValueError(f"live_recovery_receipt_manifest_missing:{stage}")
    return path, slot


def _evidence_keys(source_dir: Path, evidence_dir: Path, stage: str) -> list[str]:
    names = [
        "recovery-live/recovery-evidence.json",
        "recovery-live/dashboard-release-canary.json",
        "recovery-live/dashboard-read-model-activation.json",
    ]
    if stage in {"etf_penetration", "stock_flow", "publish"}:
        names.extend(
            [
                "recovery-live/penetration-release-canary.json",
                "recovery-live/penetration-read-model-activation.json",
                "recovery-live/stock-flow-history-status.json",
            ]
        )
    # Evidence keys are identifiers in the receipt contract.  Include only
    # files that were really emitted by this run; a PASS with a guessed path
    # would be indistinguishable from a false completion.
    available = {
        path.name
        for root in (source_dir, evidence_dir)
        if root.is_dir()
        for path in root.iterdir()
        if path.is_file()
    }
    result = [key for key in names if key.rsplit("/", 1)[-1] in available]
    if "recovery-resource-metrics.json" in available:
        result.append("recovery-live/recovery-resource-metrics.json")
    return sorted(set(result))


def _input_objects(
    *,
    source_dir: Path,
    evidence: dict[str, Any],
    target: str,
    source_fingerprint: str,
) -> list[dict[str, Any]]:
    """Return only input objects whose immutable keys were observed.

    The history-import response supplies the snapshot key produced by the
    Worker.  Stock-flow archive responses expose normalized object keys in
    the bounded fetch report.  If either response is absent we fail closed
    instead of inventing a path that was never written to R2.
    """

    objects: list[dict[str, Any]] = []
    archive = evidence.get("historyArchive")
    if isinstance(archive, dict):
        key = archive.get("snapshotKey")
        sha256 = archive.get("snapshotSha256")
        if isinstance(key, str) and isinstance(sha256, str) and len(sha256) == 64:
            objects.append(
                {
                    "key": key,
                    "sha256": sha256,
                    "sourceDates": list(evidence.get("contextDates") or [target]),
                    "sourceFingerprint": source_fingerprint,
                }
            )
    report_name = evidence.get("stockFlowInputReport")
    report_path = source_dir / report_name if isinstance(report_name, str) else None
    if report_path is not None and report_path.is_file():
        report = _read(report_path)
        for data_date, date_report in sorted((report.get("dates") or {}).items()):
            if not isinstance(date_report, dict):
                continue
            for source_status in date_report.values():
                if not isinstance(source_status, dict) or source_status.get("status") != "ready":
                    continue
                key = source_status.get("key")
                sha256 = source_status.get("sha256")
                if not isinstance(key, str) or not isinstance(sha256, str) or len(sha256) != 64:
                    raise ValueError("recovery_stock_flow_input_lineage_missing")
                objects.append(
                    {
                        "key": key,
                        "sha256": sha256,
                        "sourceDates": [str(data_date)],
                        "sourceFingerprint": source_fingerprint,
                    }
                )
    if not objects:
        raise ValueError("recovery_history_archive_lineage_missing")
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in objects:
        unique[(item["key"], item["sha256"])] = item
    return [unique[key] for key in sorted(unique)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-hash", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--producer-build-sha", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--resource-metrics", type=Path, required=True)
    parser.add_argument("--publish-run-id", required=True)
    parser.add_argument("--active-before", type=Path)
    parser.add_argument("--active-after", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    scope = validate_recovery_plan(_read(args.plan), expected_plan_hash=args.expected_plan_hash)
    if len(scope.data_dates) != 1:
        raise ValueError("live_recovery_receipts_require_one_date")
    if len(args.producer_build_sha) != 64 or any(char not in "0123456789abcdef" for char in args.producer_build_sha):
        raise ValueError("invalid_producer_build_sha")
    metrics = _read(args.resource_metrics)
    required_metrics = (
        "d1RowsRead",
        "d1RowsWritten",
        "workerRequests",
        "r2ClassA",
        "r2ClassB",
        "r2Bytes",
        "actionsMinutes",
        "measuredAt",
    )
    if any(key not in metrics for key in required_metrics) or metrics.get("measurementStatus") != "MEASURED":
        raise ValueError("recovery_resource_metrics_not_measured")
    evidence_path = args.source_dir / "recovery-evidence.json"
    if not evidence_path.is_file():
        raise ValueError("recovery_evidence_missing")
    evidence = _read(evidence_path)
    input_sha = (evidence.get("files") or {}).get("dashboard-snapshot.json", {}).get("sha256")
    if not isinstance(input_sha, str) or len(input_sha) != 64:
        raise ValueError("recovery_source_snapshot_sha_missing")
    collector_run_id = evidence.get("collectorRunId")
    if not isinstance(collector_run_id, str) or not collector_run_id:
        raise ValueError("recovery_collector_run_id_missing")
    source_fingerprint = evidence.get("sourceFingerprint")
    if not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in source_fingerprint
    ):
        raise ValueError("recovery_source_fingerprint_missing")
    target = scope.data_dates[0]
    active_before = _active_release(args.active_before)
    active_after = _active_release(args.active_after) or active_before
    output_dir = args.output_dir.resolve()
    assert_recovery_output_path(str(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, Any]] = []
    source_objects = _input_objects(
        source_dir=args.source_dir,
        evidence=evidence,
        target=target,
        source_fingerprint=source_fingerprint,
    )
    for stage in scope.required_stages:
        if stage == "pcf_exact":
            raise ValueError("live_recovery_pcf_exact_not_supported_by_bounded_runner")
        manifest_path, slot = _manifest_for_stage(args.models_dir, stage)
        manifest = _read(manifest_path)
        as_of = manifest.get("asOf")
        release_id = manifest.get("releaseId")
        manifest_sha = manifest.get("manifestSha256") or _sha256(manifest_path)
        if as_of != target or not isinstance(release_id, str) or not release_id:
            raise ValueError(f"live_recovery_receipt_output_mismatch:{stage}")
        qa_keys = _evidence_keys(args.source_dir, args.evidence_dir, stage)
        if not qa_keys:
            raise ValueError(f"live_recovery_receipt_qa_missing:{stage}")
        receipt = {
            "schemaVersion": 1,
            "campaignId": scope.campaign_id,
            "recoveryId": scope.recovery_id,
            "planHash": scope.plan_hash,
            "runKey": scope.run_key,
            "attemptCount": args.attempt,
            "dataDate": target,
            "market": "CN",
            "stage": stage,
            "status": "COMPLETED",
            "inputObjects": source_objects,
            "versions": {
                "schema": "read-model-v3",
                "calculation": "etf-stock-pressure-v2",
                "classification": "etf-classification-v2",
            },
            "producerBuildSha": args.producer_build_sha,
            "collectorRunId": collector_run_id,
            "publishRunId": args.publish_run_id,
            "output": {
                "manifestSha256": manifest_sha,
                "releaseId": release_id,
                "slot": slot,
                "asOf": as_of,
            },
            "qa": {"result": "PASS", "evidenceKeys": qa_keys},
            "historical": {
                "dateReadable": True,
                "activeBefore": active_before,
                "activeAfter": active_after,
            },
            "resources": {
                key: metrics[key]
                for key in (
                    "d1RowsRead",
                    "d1RowsWritten",
                    "workerRequests",
                    "r2ClassA",
                    "r2ClassB",
                    "r2Bytes",
                    "actionsMinutes",
                    "measuredAt",
                )
            },
            "completedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        validated = validate_recovery_receipt(receipt, scope=scope, expected_attempt=args.attempt)
        encoded, digest = receipt_bytes(validated)
        key = receipt_key(
            recovery_id=scope.recovery_id,
            data_date=target,
            stage=stage,
            attempt=args.attempt,
        )
        raw_path = output_dir / f"{stage}.json"
        meta_path = output_dir / f"{stage}.meta.json"
        raw_path.write_bytes(encoded)
        metadata = {
            "schemaVersion": 1,
            "status": "valid",
            "key": key,
            "sha256": digest,
            "bytes": len(encoded),
            "stage": stage,
            "dataDate": target,
            "attemptCount": args.attempt,
        }
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        generated.append(metadata)
    index = output_dir / "index.json"
    index.write_text(json.dumps({"schemaVersion": 1, "receipts": generated}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "valid", "count": len(generated), "receipts": generated}, ensure_ascii=False))


if __name__ == "__main__":
    main()
