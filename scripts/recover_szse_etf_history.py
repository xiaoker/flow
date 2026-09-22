"""Recover SZSE ETF scale rows from the exchange's retained history endpoint.

The public SZSE fund list only exposes the latest workbook.  Its per-fund
``fund_jjgm`` endpoint still retains historical daily scale rows, so this
script replays those original responses into one immutable evidence file per
trade date and a normalized CSV that can be checked before publication.

This is deliberately a source-recovery tool, not a derived-value generator:
shares are parsed from the exchange's ``current_size`` value in ten-thousand
fund units and the original response is retained verbatim in the raw JSON.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


PAGE_URL = "https://fund.szse.cn/marketdata/fundslist/index.html"
WWW_PAGE_URL = "https://www.szse.cn/market/product/fund/etf/"
REPORT_URL = "https://fund.szse.cn/api/report/ShowReport/data"
WWW_REPORT_URL = "https://www.szse.cn/api/report/ShowReport/data"
# Optional resolver override for environments whose DNS path is unreliable.
# Do not bake a volatile exchange edge address into the recovery contract;
# leaving this unset lets curl resolve the current official public address.
SZSE_RESOLVE_ENV = "FLOW_RECOVERY_SZSE_RESOLVE"
SZSE_HOST_ENV = "FLOW_RECOVERY_SZSE_HOST"
SZSE_HOSTS = ("www.szse.cn", "fund.szse.cn")
LIST_PARAMS = {
    "SHOWTYPE": "JSON",
    "CATALOGID": "1000_lf",
    "TABKEY": "tab1",
    "tab1PAGESIZE": "20",
}
DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
CODE_RE = re.compile(r"\d{6}")


class RequestBudget:
    """Bound and pace every top-level exchange request in this helper."""

    def __init__(self, maximum: int | None, min_interval_seconds: float) -> None:
        if maximum is not None and maximum <= 0:
            raise ValueError("max_requests must be positive")
        if not math.isfinite(min_interval_seconds) or min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be non-negative")
        self.maximum = maximum
        self.min_interval_seconds = min_interval_seconds
        self.request_count = 0
        self._last_request_at = 0.0
        self._lock = threading.Lock()
        self._active_host: str | None = None

    def before_request(self) -> None:
        with self._lock:
            if self.maximum is not None and self.request_count >= self.maximum:
                raise RuntimeError("recovery_request_budget_exceeded")
            now = time.monotonic()
            delay = self.min_interval_seconds - (now - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            self.request_count += 1
            self._last_request_at = time.monotonic()

    def host_order(self) -> tuple[str, ...]:
        configured = os.environ.get(SZSE_HOST_ENV, "").strip()
        if configured and configured not in SZSE_HOSTS:
            raise ValueError("invalid_szse_host")
        if os.environ.get(SZSE_RESOLVE_ENV, "").strip():
            return ("fund.szse.cn",)
        preferred = configured or SZSE_HOSTS[0]
        with self._lock:
            active = self._active_host
        if active:
            return (active,) + tuple(host for host in SZSE_HOSTS if host != active)
        return (preferred,) + tuple(host for host in SZSE_HOSTS if host != preferred)

    def mark_host_success(self, host: str) -> None:
        with self._lock:
            self._active_host = host

    @property
    def active_host(self) -> str | None:
        with self._lock:
            return self._active_host


def _report_url(host: str) -> str:
    return WWW_REPORT_URL if host == "www.szse.cn" else REPORT_URL


def _page_url(host: str) -> str:
    return WWW_PAGE_URL if host == "www.szse.cn" else PAGE_URL


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "flow-data-recovery/1.0 (+https://flow.xiaoker.com)",
            "Referer": PAGE_URL,
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session


def _request_json(
    session: requests.Session,
    params: dict[str, str],
    *,
    budget: RequestBudget | None = None,
) -> Any:
    # The desktop proxy intermittently returns a 503 tunnel error for a burst
    # of Python TLS sessions, while curl's connection reuse/retry path remains
    # stable.  Keep this recovery path conservative and invoke curl with an
    # argument list (never a shell string) so all query values are escaped.
    resolve = os.environ.get(SZSE_RESOLVE_ENV, "").strip()
    if resolve and not re.fullmatch(r"fund\.szse\.cn:443:(?:\d{1,3}\.){3}\d{1,3}", resolve):
        raise RuntimeError("invalid_szse_resolver")
    if budget is not None:
        hosts = budget.host_order()
    else:
        configured = os.environ.get(SZSE_HOST_ENV, "").strip()
        if configured and configured not in SZSE_HOSTS:
            raise RuntimeError("invalid_szse_host")
        if resolve:
            hosts = ("fund.szse.cn",)
        else:
            preferred = configured or "www.szse.cn"
            hosts = (preferred,) + tuple(host for host in SZSE_HOSTS if host != preferred)
    if any(host not in SZSE_HOSTS for host in hosts):
        raise RuntimeError("invalid_szse_host")
    failures: list[str] = []
    for host in hosts:
        if budget is not None:
            try:
                budget.before_request()
            except RuntimeError as exc:
                if str(exc) == "recovery_request_budget_exceeded":
                    failures.append("budget_exhausted")
                    break
                raise
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--retry",
            "0",
            "--max-time",
            "30",
            "-A",
            "Mozilla/5.0",
            "-H",
            f"Referer: {_page_url(host)}",
            "--get",
        ]
        if resolve:
            command[8:8] = ["--resolve", resolve]
        for key, value in params.items():
            command.extend(["--data-urlencode", f"{key}={value}"])
        endpoint = _report_url(host)
        command.append(endpoint)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            failures.append(f"{host}:timeout")
            continue
        except OSError:
            failures.append(f"{host}:unavailable")
            continue
        if result.returncode != 0:
            failures.append(f"{host}:exit={result.returncode}")
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            failures.append(f"{host}:invalid_json")
            continue
        if budget is not None:
            budget.mark_host_success(host)
        return payload
    raise RuntimeError("SZSE history request failed: " + ",".join(failures))


def _strip_anchor(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _code_from_row(row: dict[str, Any]) -> str | None:
    match = CODE_RE.search(_strip_anchor(row.get("sys_key")))
    return match.group(0) if match else None


def _current_list(
    budget: RequestBudget | None = None,
    *,
    max_workers: int = 1,
) -> dict[str, dict[str, str]]:
    if max_workers < 1 or max_workers > 8:
        raise ValueError("max_workers must be between 1 and 8")
    first = _request_json(_session(), {**LIST_PARAMS, "tab1PAGENO": "1"}, budget=budget)
    metadata = first[0]["metadata"]
    page_count = int(metadata["pagecount"])
    pages: list[list[dict[str, Any]]] = [first[0].get("data") or []]

    def fetch_page(page: int) -> list[dict[str, Any]]:
        payload = _request_json(
            _session(), {**LIST_PARAMS, "tab1PAGENO": str(page)}, budget=budget
        )
        return payload[0].get("data") or []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_page, page) for page in range(2, page_count + 1)]
        for future in futures:
            pages.append(future.result())

    result: dict[str, dict[str, str]] = {}
    for page in pages:
        for row in page:
            code = _code_from_row(row)
            if not code:
                continue
            result[code] = {
                "instrument_code": code,
                "instrument_name": _strip_anchor(row.get("jjjcurl")),
                "etf_type": _strip_anchor(row.get("jjlb")),
                "investment_type": _strip_anchor(row.get("tzlb")),
            }
    if len(result) < int(metadata["recordcount"]):
        raise RuntimeError(
            f"SZSE list pagination incomplete: {len(result)} != {metadata['recordcount']}"
        )
    return result


def _load_code_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("code override file must contain an array")
    result: dict[str, dict[str, str]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        code = str(value.get("instrument_code") or "").strip().zfill(6)
        if not CODE_RE.fullmatch(code):
            continue
        result[code] = {
            "instrument_code": code,
            "instrument_name": str(value.get("instrument_name") or "").strip(),
            "etf_type": "ETF",
            "investment_type": str(value.get("category") or "").strip(),
        }
    return result


def _historical_response(
    code: str,
    start_date: str,
    end_date: str,
    *,
    budget: RequestBudget | None = None,
) -> tuple[str, dict[str, Any]]:
    payload = _request_json(
        _session(),
        {
            "SHOWTYPE": "JSON",
            "CATALOGID": "fund_jjgm",
            "TABKEY": "tab1",
            "loading": "first",
            "txtDm": code,
            "txtStart": start_date,
            "txtEnd": end_date,
        },
        budget=budget,
    )
    host = budget.active_host if budget is not None and budget.active_host else (
        "fund.szse.cn" if os.environ.get(SZSE_RESOLVE_ENV, "").strip() else "www.szse.cn"
    )
    return code, {
        "request": {"code": code, "start": start_date, "end": end_date},
        "response": payload,
        "source_url": _report_url(host),
    }


def _number_of_shares(value: Any) -> int:
    text = str(value or "").replace(",", "").strip()
    number = float(text)
    shares = number * 10_000
    rounded = round(shares)
    if abs(shares - rounded) > 0.01:
        raise ValueError(f"non-integral SZSE share count: {value!r}")
    return int(rounded)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--code-overrides", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--min-interval", type=float, default=0.5)
    args = parser.parse_args()
    dates = sorted(set(args.dates))
    if not dates or any(not DATE_RE.fullmatch(value) for value in dates):
        raise ValueError("all dates must be ISO dates")
    start_date, end_date = dates[0], dates[-1]
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    response_root = output_root / "responses"
    response_root.mkdir(parents=True, exist_ok=True)
    budget = RequestBudget(args.max_requests, args.min_interval)

    overrides = _load_code_overrides(args.code_overrides)
    # A D1 export from the last known release is a safer and much cheaper
    # starting universe during recovery.  Only fall back to the paginated live
    # list when no substantial override set was supplied.
    worker_count = max(1, min(args.workers, 8))
    codes = dict(overrides) if len(overrides) >= 500 else _current_list(
        budget,
        max_workers=worker_count,
    )
    if codes is not overrides:
        codes.update(overrides)
    ordered_codes = sorted(codes)
    responses: dict[str, dict[str, Any]] = {}
    pending_codes: list[str] = []
    for code in ordered_codes:
        cache = response_root / f"{code}.json"
        if cache.is_file():
            responses[code] = json.loads(cache.read_text(encoding="utf-8"))
        else:
            pending_codes.append(code)
    print(
        f"SZSE history cache: {len(responses)} ready, {len(pending_codes)} pending",
        flush=True,
    )

    def fetch_and_cache(code: str) -> str:
        _, payload = _historical_response(code, start_date, end_date, budget=budget)
        (response_root / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return code

    failed: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(fetch_and_cache, code): code for code in pending_codes}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            code = futures[future]
            try:
                future.result()
                responses[code] = json.loads(
                    (response_root / f"{code}.json").read_text(encoding="utf-8")
                )
            except Exception as exc:
                failed.append(code)
                print(f"deferred {code}: {exc}", flush=True)
            if index % 50 == 0 or index == len(futures):
                print(
                    f"fetched {index}/{len(futures)} SZSE fund histories; failed={len(failed)}",
                    flush=True,
                )
    # The exchange occasionally rejects a burst of connections.  Retry failed
    # codes at a low rate rather than silently publishing an incomplete set.
    for code in failed:
        time.sleep(0.25)
        _, payload = _historical_response(code, start_date, end_date, budget=budget)
        (response_root / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        responses[code] = payload
    if len(responses) != len(ordered_codes):
        missing = sorted(set(ordered_codes) - set(responses))
        raise RuntimeError(f"SZSE history recovery incomplete: {missing[:20]} ({len(missing)} missing)")

    observed_at = _now()
    transport_host = budget.active_host or (
        "fund.szse.cn" if os.environ.get(SZSE_RESOLVE_ENV, "").strip() else "www.szse.cn"
    )
    transport_report_url = _report_url(transport_host)
    transport_page_url = _page_url(transport_host)
    artifacts: list[dict[str, Any]] = []
    for trade_date in dates:
        rows: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        for code in ordered_codes:
            payload = responses[code]
            response = payload["response"]
            raw_rows.append(payload)
            data = response[0].get("data") if isinstance(response, list) and response else []
            if not isinstance(data, list):
                continue
            matching = [item for item in data if str(item.get("size_date")) == trade_date]
            if len(matching) > 1:
                raise RuntimeError(f"duplicate SZSE history rows for {code}/{trade_date}")
            if not matching:
                continue
            item = matching[0]
            identity = codes[code]
            name = str(item.get("security_short_name") or identity.get("instrument_name") or "").strip()
            if not name:
                raise RuntimeError(f"missing SZSE ETF name for {code}/{trade_date}")
            rows.append(
                {
                    "trade_date": trade_date,
                    "market": "SZSE",
                    "instrument_code": code,
                    "instrument_name": name,
                    "etf_type": identity.get("etf_type") or "ETF",
                    "investment_type": identity.get("investment_type") or "",
                    "shares": _number_of_shares(item.get("current_size")),
                    "nav": "",
                    "nav_date": "",
                    "date_basis": "source_historical_scale_size_date",
                    "observed_at": observed_at,
                }
            )
        if not rows:
            raise RuntimeError(f"SZSE endpoint returned no rows for {trade_date}")
        codes_seen = {row["instrument_code"] for row in rows}
        raw_payload = {
            "source": "深圳证券交易所",
            "dataset": "etf_scale",
            "endpoint": transport_report_url,
            "page": transport_page_url,
            "requestedDate": trade_date,
            "queryRange": {"start": start_date, "end": end_date},
            "retrievedAt": observed_at,
            "codeCount": len(ordered_codes),
            "matchingRowCount": len(rows),
            "responses": raw_rows,
        }
        raw_content = json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw_content).hexdigest()
        directory = output_root / trade_date
        directory.mkdir(parents=True, exist_ok=True)
        raw_path = directory / f"{digest}.json"
        raw_path.write_bytes(raw_content)
        metadata = {
            "version": 1,
            "source": {
                "source_id": "szse",
                "publisher": "深圳证券交易所",
                "dataset": "etf_scale",
                "url": transport_report_url,
                "source_grade": "A",
                "date_semantics": "source_historical_scale_size_date",
            },
            "source_id": "szse",
            "publisher": "深圳证券交易所",
            "dataset": "etf_scale",
            "source_url": transport_report_url,
            "source_grade": "A",
            "page_url": transport_page_url,
            "requested_date": trade_date,
            "date_semantics": "source_historical_scale_size_date",
            "query_range": {"start": start_date, "end": end_date},
            "row_count": len(rows),
            "code_count": len(codes_seen),
            "sha256": digest,
            "observed_at": observed_at,
        }
        metadata_path = directory / f"{digest}.metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        csv_path = directory / "data.csv"
        fields = list(rows[0])
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            import csv

            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        artifacts.append(
            {
                "source": {
                    "source_id": "szse",
                    "publisher": "深圳证券交易所",
                    "dataset": "etf_scale",
                    "url": transport_report_url,
                    "source_grade": "A",
                    "date_semantics": "source_historical_scale_size_date",
                },
                "requested_date": trade_date,
                "observed_at": observed_at,
                "raw_path": str(raw_path),
                "metadata_path": str(metadata_path),
                "normalized_path": str(csv_path),
                "sha256": digest,
                "content_type": "application/json; charset=utf-8",
            }
        )
        print(f"prepared {trade_date}: rows={len(rows)} codes={len(codes_seen)} sha256={digest}", flush=True)

    manifest = {
        "version": 1,
        "artifacts": artifacts,
        "dates": dates,
        "observed_at": observed_at,
        "requestCount": budget.request_count,
        "maxRequests": args.max_requests,
        "minIntervalSeconds": args.min_interval,
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "dates": dates,
                "artifacts": len(artifacts),
                "codes": len(ordered_codes),
                "requestCount": budget.request_count,
                "output": str(output_root),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
