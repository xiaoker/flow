"""Aggregate measured resource counters for one recovery publish.

This is intentionally a conservative local accounting step.  It consumes
only counters emitted by the recovery runner/publisher and the elapsed job
clock; it never queries D1/R2 or treats missing values as a measured zero.
The output is an input to receipt creation, not a claim that the account has
unlimited quota.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"metrics object required: {path}")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid measured {field}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-epoch", type=Path, required=True)
    parser.add_argument("--source-evidence", type=Path, required=True)
    parser.add_argument("--control-response", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    started = int(args.start_epoch.read_text(encoding="utf-8").strip())
    now = int(time.time())
    if started <= 0 or now < started:
        raise ValueError("invalid publish elapsed clock")
    source = _read(args.source_evidence)
    control = _read(args.control_response)
    source_requests = _integer(source.get("requestCount"), "sourceRequestCount")
    source_bundle_bytes = sum(
        _integer(item.get("bytes"), "sourceFileBytes")
        for item in (source.get("files") or {}).values()
        if isinstance(item, dict) and "bytes" in item
    )
    # The control response is from the authenticated lease mutation.  It is
    # the only D1 physical-write counter exposed to this bounded workflow;
    # a deployed endpoint that omits it is not silently converted to zero.
    d1_written = _integer(control.get("rowsWritten"), "d1RowsWritten")
    d1_read = control.get("rowsRead")
    if d1_read is None:
        raise ValueError("d1RowsRead_not_measured_by_control_endpoint")
    d1_read = _integer(d1_read, "d1RowsRead")

    if not args.metrics:
        raise ValueError("recovery_stage_metrics_missing")

    totals = {
        "workerRequests": source_requests,
        "r2ClassA": 0,
        "r2ClassB": 0,
        # ``r2Bytes`` below is the measured client payload transferred to
        # Worker-backed R2 writes.  The source bundle itself is an Actions
        # artifact and must not be mistaken for R2 storage usage.
        "r2Bytes": 0,
    }
    stage_metrics: list[dict[str, Any]] = []
    for path in args.metrics:
        metrics = _read(path)
        if metrics.get("measurementStatus") not in (None, "MEASURED"):
            raise ValueError(f"invalid_stage_measurement_status:{path}")
        missing = [field for field in totals if field not in metrics]
        if missing:
            raise ValueError(f"stage_metrics_missing:{path}:{','.join(missing)}")
        for field in totals:
            value = metrics[field]
            totals[field] += _integer(value, field)
        stage_metrics.append({"path": str(path), **{field: metrics[field] for field in totals}})

    elapsed = now - started
    result = {
        "schemaVersion": 1,
        "measurementStatus": "MEASURED",
        "d1RowsRead": d1_read,
        "d1RowsWritten": d1_written,
        "workerRequests": totals["workerRequests"],
        "r2ClassA": totals["r2ClassA"],
        "r2ClassB": totals["r2ClassB"],
        "r2Bytes": totals["r2Bytes"],
        "actionsMinutes": max(1, math.ceil(elapsed / 60)),
        "elapsedSeconds": elapsed,
        "sourceBundleBytes": source_bundle_bytes,
        "measurementBasis": {
            "d1": "authenticated_control_response_meta_rows_read_rows_written",
            "workerRequests": "source_evidence_request_count_plus_stage_metric_requests",
            "r2": "stage_metric_client_payload_bytes_and_operation_counters",
            "sourceBundleBytes": "local_artifact_bytes_not_r2_usage",
            "stageMetrics": stage_metrics,
        },
        "measuredAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
