from __future__ import annotations

from pathlib import Path

import pytest

from scripts.inventory_stock_flow_inputs import _dates


def test_inventory_dates_are_sorted_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-08-18\n2026-08-17\n2026-08-18\n", encoding="utf-8")
    assert _dates(path) == ["2026-08-17", "2026-08-18"]


def test_inventory_rejects_non_iso_dates(tmp_path: Path) -> None:
    path = tmp_path / "dates.txt"
    path.write_text("2026-08-18\nnot-a-date\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ISO date"):
        _dates(path)
