from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "data-lineage" / "etf-name-code-check.csv"


def main() -> None:
    snapshot = json.loads((ROOT / "dashboard-snapshot.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "reports" / "phase0" / "latest-manifest.json").read_text(encoding="utf-8")
    )
    run_id = manifest["run_id"]
    paths = manifest["combined_paths"]

    sse = pd.read_csv(ROOT / paths["sse_etf"], dtype={"instrument_code": str})
    szse = pd.read_csv(ROOT / paths["szse_etf"], dtype={"instrument_code": str})
    latest_date = str(snapshot["asOf"])
    sse["instrument_code"] = sse["instrument_code"].astype(str).str.zfill(6)
    szse["instrument_code"] = szse["instrument_code"].astype(str).str.zfill(6)
    sse_latest = (
        sse.loc[sse["trade_date"].astype(str).eq(latest_date)]
        .drop_duplicates("instrument_code", keep="last")
        .set_index("instrument_code")
    )
    sse_last_seen = (
        sse.sort_values("trade_date")
        .drop_duplicates("instrument_code", keep="last")
        .set_index("instrument_code")
    )
    szse_current = szse.drop_duplicates("instrument_code", keep="last").set_index(
        "instrument_code"
    )

    szse_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["source"]["source_id"] == "szse"
        and item["source"]["dataset"] == "etf_scale"
    )
    szse_raw = pd.read_excel(
        ROOT / szse_artifact["raw_path"],
        engine="openpyxl",
        dtype={"基金代码": str},
    )
    szse_raw["基金代码"] = szse_raw["基金代码"].astype(str).str.zfill(6)
    szse_listing_dates = (
        szse_raw.drop_duplicates("基金代码", keep="last")
        .set_index("基金代码")["上市日期"]
        .astype(str)
        .to_dict()
    )

    rows: list[dict[str, object]] = []
    for item in snapshot["etfTerminal"]["instruments"]:
        code = str(item["code"]).zfill(6)
        market = str(item.get("market") or "SSE")
        if market == "SSE":
            current = sse_latest.loc[code] if code in sse_latest.index else None
            source = current if current is not None else sse_last_seen.loc[code]
            source_status = "当日官方行" if current is not None else "仅历史行，当日已缺失"
            source_date = str(source["trade_date"])
            date_basis = "source_published"
            code_field = "SEC_CODE"
            name_field = "SEC_NAME"
            shares_field = "TOT_VOL"
            source_unit = "万份"
            listing_date = ""
        else:
            source = szse_current.loc[code]
            source_status = "官方列表页规模日期"
            source_date = str(source["trade_date"])
            date_basis = str(source["date_basis"])
            code_field = "基金代码"
            name_field = "基金简称"
            shares_field = "当前规模(份)"
            source_unit = "份"
            listing_date = szse_listing_dates.get(code, "")

        source_name = str(source["instrument_name"])
        source_shares = float(source["shares"])
        page_shares = item.get("shares")
        expected_page_shares = round(source_shares / 100_000_000, 2)
        shares_match = (
            page_shares is not None
            and abs(float(page_shares) - expected_page_shares) <= 0.011
        )
        if market == "SSE" and current is None and page_shares is None:
            shares_match = True

        rows.append(
            {
                "market": "上交所" if market == "SSE" else "深交所",
                "page_code": code,
                "page_name": item["name"],
                "page_category": item["category"],
                "classification_source": item.get("classificationSource", ""),
                "source_code_field": code_field,
                "source_name_field": name_field,
                "source_code": code,
                "source_name": source_name,
                "source_shares_field": shares_field,
                "source_shares_unit": source_unit,
                "source_shares_normalized": source_shares,
                "page_shares_100m": page_shares,
                "source_date": source_date,
                "source_page_date": (
                    "" if market == "SSE" else str(source.get("source_page_date", ""))
                ),
                "date_basis": date_basis,
                "official_listing_date": listing_date,
                "latest_source_status": source_status,
                "code_match": code == str(item["code"]).zfill(6),
                "name_match": source_name == str(item["name"]),
                "shares_match": shares_match,
                "manual_check": "",
                "audit_run_id": run_id,
            }
        )

    output = pd.DataFrame(rows).sort_values(["market", "page_code"])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(
        json.dumps(
            {
                "path": str(OUTPUT.relative_to(ROOT)),
                "rows": len(output),
                "code_mismatches": int((~output["code_match"]).sum()),
                "name_mismatches": int((~output["name_match"]).sum()),
                "share_mismatches": int((~output["shares_match"]).sum()),
                "missing_latest_sse": int(
                    output["latest_source_status"].eq("仅历史行，当日已缺失").sum()
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
