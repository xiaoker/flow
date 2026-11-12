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


@pytest.mark.parametrize("status", ["MEASURED", "UNKNOWN"])
def test_preparation_physical_counts_are_added_without_fabricating_measurements(tmp_path, monkeypatch, status):
    start = tmp_path / "start.txt"
    start.write_text("1000\n")
    source, control, stage, output = [tmp_path / name for name in
        ("source.json", "control.json", "preparation.json", "output.json")]
    _write(source, {"requestCount": 0, "files": {}})
    _write(control, {"rowsRead": 4, "rowsWritten": 2})
    _write(stage, {"measurementStatus": status, "workerRequests": 46,
        "r2Bytes": 0, "r2ClassA": 2, "r2ClassB": 44,
        "d1RowsRead": 44 if status == "MEASURED" else None, "d1RowsWritten": 0})
    monkeypatch.setattr(measure_recovery_resources.time, "time", lambda: 1061)
    monkeypatch.setattr(sys, "argv", ["measure", "--start-epoch", str(start),
        "--source-evidence", str(source), "--control-response", str(control),
        "--metrics", str(stage), "--output", str(output)])
    if status == "UNKNOWN":
        with pytest.raises(ValueError, match="invalid_stage_measurement_status"):
            measure_recovery_resources.main()
        assert not output.exists()
    else:
        measure_recovery_resources.main()
        result = json.loads(output.read_bytes())
        assert result["d1RowsRead"] == 48 and result["d1RowsWritten"] == 2
        assert result["workerRequests"] == 46 and result["r2ClassB"] == 44
