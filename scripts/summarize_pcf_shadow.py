"""Summarize PR4 shadow reports and enforce the five-trading-day gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.pcf_parity import summarize_pcf_parity_reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize SSE ETF PCF shadow parity reports")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/pcf-shadow/summary.json"))
    parser.add_argument("--required-days", type=int, default=5)
    args = parser.parse_args()
    reports = []
    for path in sorted(args.input_dir.glob("*.json")):
        if path.name == args.output.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("shadowOnly") is True and payload.get("tradeDate"):
            reports.append(payload)
    summary = summarize_pcf_parity_reports(reports, required_days=args.required_days)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": summary["status"], "observedDays": summary["observedDays"]}, ensure_ascii=False))
    if summary["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
