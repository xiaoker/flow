"""One indexed previous-day SZSE archive input, shared by both producers."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import re
import pandas as pd
import requests

MAX_BYTES = 2_000_000

def load_archived_frame(reference, root):
    required = {"sourceId", "tradeDate", "objectKey", "sha256", "revisionId", "normalizedPath"}
    if not isinstance(reference, dict) or set(reference) != required or reference["sourceId"] != "szse":
        raise ValueError("adjacent_archive_identity_invalid")
    day = reference["tradeDate"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"]):
        raise ValueError("adjacent_archive_identity_invalid")
    revision = reference["revisionId"]
    if not re.fullmatch(r"[a-f0-9]{64}", revision) or reference["objectKey"] != f"market-data/normalized/szse/etf_scale/{day}/{revision}/data.csv":
        raise ValueError("adjacent_archive_identity_invalid")
    root = Path(root).resolve()
    path = root / reference["normalizedPath"]
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("adjacent_archive_path_invalid")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("adjacent_archive_hash_mismatch")
    frame = pd.read_csv(io.BytesIO(data), dtype={"instrument_code": str})
    if frame.empty or len(frame) > 5000 or not {"trade_date", "market", "instrument_code", "shares"}.issubset(frame):
        raise ValueError("adjacent_archive_rows_invalid")
    if set(frame.trade_date) != {day} or set(frame.market) != {"SZSE"} or frame.instrument_code.duplicated().any():
        raise ValueError("adjacent_archive_rows_invalid")
    return frame

def archived_inputs(manifest, root, as_of):
    refs = manifest.get("archivedEtfInputs", [])
    if not isinstance(refs, list) or len(refs) > 1:
        raise ValueError("adjacent_archive_count_invalid")
    if not refs:
        return []  # Legacy manifests need no new date/receipt fields.
    dates = sorted(manifest["requested_dates"])
    previous = max((d for d in dates if d < as_of), default=None)
    result = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("tradeDate") != previous:
            raise ValueError("adjacent_archive_date_mismatch")
        result.append((ref, load_archived_frame(ref, root)))
    return result

def main():
    root = Path.cwd()
    path = root / "reports/phase0/latest-manifest.json"
    manifest = json.loads(path.read_bytes())
    current_path = manifest.get("combined_paths", {}).get("szse_etf")
    if not current_path:
        print("adjacent_archive_no_current"); return
    frame = pd.read_csv(root / current_path, dtype={"instrument_code": str})
    if frame.empty or "trade_date" not in frame:
        print("adjacent_archive_no_current"); return
    dates = sorted(manifest["requested_dates"])
    current = max((str(d) for d in frame.trade_date if str(d) <= dates[-1]), default=None)
    if current != dates[-1]:
        print("adjacent_archive_source_pending"); return
    previous = max((d for d in dates if current and d < current), default=None)
    if not previous or previous in set(frame.trade_date):
        print("adjacent_archive_not_required"); return
    headers = {"Authorization": "Bearer " + os.environ["FLOW_INGEST_API_KEY"],
        "CF-Access-Client-Id": os.environ["FLOW_CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["FLOW_CF_ACCESS_CLIENT_SECRET"]}
    # One request, fixed origin, indexed-only selection, no redirect/retry.
    with requests.get("https://flow.xiaoker.com/api/v1/admin/history-artifact", params={"source":"szse",
            "dataset":"etf_scale", "date":previous, "file":"data", "indexed":"1"}, headers=headers,
            stream=True, timeout=(8,20), allow_redirects=False) as response:
        if response.status_code == 404:
            print("adjacent_archive_missing_previous"); return
        if response.status_code != 200:
            raise ValueError("adjacent_archive_control_rejected")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES: raise ValueError("adjacent_archive_size_exceeded")
        ref = {"sourceId":"szse", "tradeDate":previous,
            "objectKey":response.headers.get("x-flow-normalized-key", ""),
            "sha256":response.headers.get("x-flow-normalized-sha256", ""),
            "revisionId":response.headers.get("x-flow-pcf-revision", ""),
            "normalizedPath":"reports/phase0/adjacent-szse.csv"}
    destination = root / ref["normalizedPath"]
    if destination.exists(): raise ValueError("adjacent_archive_existing_input")
    destination.write_bytes(data)
    load_archived_frame(ref, root)
    manifest["archivedEtfInputs"] = [ref]
    path.write_text(json.dumps(manifest, ensure_ascii=False))
    print(json.dumps({"status":"adjacent_archive_verified", "date":previous, "requests":1}))

if __name__ == "__main__":
    main()
