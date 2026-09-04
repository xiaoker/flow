from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import measure_recovery_resources


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def test_measurement_requires_explicit_stage_metrics_and_separates_artifact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = tmp_path / "start.txt"
    start.write_text("1000\n", encoding="utf-8")
    source = tmp_path / "source.json"
    _write(
        source,
        {
            "requestCount": 3,
            "files": {"dashboard-snapshot.json": {"bytes": 12}},
        },
    )
    control = tmp_path / "control.json"
    _write(control, {"rowsRead": 4, "rowsWritten": 2})
    stage = tmp_path / "stage.json"
    _write(stage, {"workerRequests": 5, "r2Bytes": 7, "r2ClassA": 1, "r2ClassB": 2})
    output = tmp_path / "metrics.json"
    monkeypatch.setattr(measure_recovery_resources.time, "time", lambda: 1061)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "measure_recovery_resources.py",
            "--start-epoch",
            str(start),
            "--source-evidence",
            str(source),
            "--control-response",
            str(control),
            "--metrics",
            str(stage),
            "--output",
            str(output),
        ],
    )

    measure_recovery_resources.main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["workerRequests"] == 8
    assert result["r2Bytes"] == 7
    assert result["sourceBundleBytes"] == 12
    assert result["measurementBasis"]["sourceBundleBytes"] == "local_artifact_bytes_not_r2_usage"


def test_measurement_rejects_missing_counter_in_stage_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = tmp_path / "start.txt"
    start.write_text("1000\n", encoding="utf-8")
    source = tmp_path / "source.json"
    _write(source, {"requestCount": 0, "files": {}})
    control = tmp_path / "control.json"
    _write(control, {"rowsRead": 0, "rowsWritten": 0})
    stage = tmp_path / "stage.json"
    _write(stage, {"workerRequests": 1, "r2Bytes": 0, "r2ClassA": 0})
    monkeypatch.setattr(measure_recovery_resources.time, "time", lambda: 1001)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "measure_recovery_resources.py",
            "--start-epoch",
            str(start),
            "--source-evidence",
            str(source),
            "--control-response",
            str(control),
            "--metrics",
            str(stage),
            "--output",
            str(tmp_path / "metrics.json"),
        ],
    )
    with pytest.raises(ValueError, match="stage_metrics_missing"):
        measure_recovery_resources.main()
