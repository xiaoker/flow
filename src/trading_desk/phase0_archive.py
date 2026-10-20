"""Approved, exact-object archive input for the existing phase0 collector.

No scheduling, claim, registration, publishing, fallback or official fetches.
The enclosing recovery plan binds this recipe; this module checks every byte
and reuses the existing lineage/mapping QA before exposing adapter results.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import os
import tempfile
import zlib
from pathlib import Path, PurePosixPath

import pandas as pd

from .etf_period_dependencies import validate_official_calendar
from .etf_archive_adjacent import archived_source_release_id
from .models import FetchArtifact, SourceSpec
from .release_qa import validate_artifact_lineage, validate_raw_to_normalized_mapping

CONTRACT = "flow-phase0-archive-source-v1"
MAX_FILES = 96
MAX_OBJECT_BYTES = 2_000_000
MAX_TOTAL_BYTES = 32_000_000
_SHA = re.compile(r"[a-f0-9]{64}")
_PAIRS = {("sse", d) for d in ("etf_scale", "etf_nav", "etf_pcf", "margin_market_daily", "margin_security_daily")} | {
    ("szse", "etf_scale"), ("szse", "margin_market_daily"), ("bse", "margin_market_daily"),
    ("hkex", "stock_connect_daily")}


def _require(condition, code="archive_phase0_recipe_invalid"):
    if not condition:
        raise ValueError(code)


def _safe_path(value):
    _require(isinstance(value, str) and 0 < len(value) <= 500 and "\\" not in value)
    path = PurePosixPath(value)
    _require(not path.is_absolute() and not any(p in {"", ".", ".."} for p in value.split("/")))
    return path


def validate_recipe(recipe, target):
    """Bound the entire approved graph before the first archive request."""
    _require(isinstance(recipe, dict) and set(recipe) == {
        "contract", "manifestGzipBase64", "manifestSha256", "calendar", "files"} and recipe["contract"] == CONTRACT)
    _require(isinstance(recipe["manifestSha256"], str) and _SHA.fullmatch(recipe["manifestSha256"]))
    encoded = recipe["manifestGzipBase64"]
    _require(isinstance(encoded, str) and len(encoded) <= 128_000)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded, validate=True))) as stream:
            content = stream.read(256_001)
        _require(len(content) <= 256_000 and hashlib.sha256(content).hexdigest() == recipe["manifestSha256"])
        manifest = json.loads(content)
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise ValueError("archive_phase0_manifest_invalid") from exc
    dates, _ = validate_official_calendar(recipe["calendar"])
    _require(isinstance(manifest, dict) and isinstance(manifest.get("requested_dates"), list))
    requested = manifest["requested_dates"]
    _require(len(requested) == 3 and requested == sorted(set(requested)) and requested[-1] == target
        and target in dates and requested == dates[dates.index(target)-2:dates.index(target)+1], "archive_phase0_dates_invalid")
    artifacts = manifest.get("artifacts")
    _require(isinstance(artifacts, list) and 1 <= len(artifacts) <= 24)
    expected = {}
    identities = set()
    for item in artifacts:
        _require(isinstance(item, dict) and isinstance(item.get("source"), dict))
        source, dataset = item["source"].get("source_id"), item["source"].get("dataset")
        day, raw_sha = item.get("requested_date"), item.get("sha256")
        _require(isinstance(source, str) and isinstance(dataset, str) and (source, dataset) in _PAIRS and isinstance(day, str)
            and day in {*requested, f"{requested[0]}_{requested[-1]}"}
            and isinstance(raw_sha, str) and _SHA.fullmatch(raw_sha))
        _require((source, dataset, day) not in identities)
        identities.add((source, dataset, day))
        raw_path, metadata_path = str(_safe_path(item.get("raw_path"))), str(_safe_path(item.get("metadata_path")))
        extension = PurePosixPath(raw_path).suffix.lstrip(".")
        _require(extension in {"json", "js", "jsonp", "xlsx", "gz"})
        prefix = f"market-data/raw/{source}/{dataset}/{day}/{raw_sha}"
        pairs = [(raw_path, prefix+"."+extension, "raw"), (metadata_path, prefix+".metadata.json", "metadata")]
        paths = item.get("normalized_path")
        _require(isinstance(paths, str))
        normalized = paths.split(";")
        _require(1 <= len(normalized) <= 2 and (len(normalized) == 1 or dataset == "stock_connect_daily"))
        names = ["summary", "top"] if len(normalized) == 2 else ["data"]
        for path, name in zip(normalized, names, strict=True):
            pairs.append((str(_safe_path(path)), f"market-data/normalized/{source}/{dataset}/{day}/", name))
        for path, key, kind in pairs:
            _require(path not in expected and path.startswith(("data/raw/", "data/normalized/")))
            expected[path] = (key, kind, raw_sha, dataset)
    # The existing full-window collector calls exactly these source slots.
    # A missing required slot is detectable before any remote read or claim.
    required = {("sse", "etf_scale", d) for d in requested} | {
        ("szse", "etf_scale", target), ("sse", "margin_market_daily", f"{requested[0]}_{target}")}
    required |= {(s, dataset, d) for d in requested for s, dataset in (
        ("szse", "margin_market_daily"), ("bse", "margin_market_daily"),
        ("hkex", "stock_connect_daily"))}
    _require(required.issubset(identities), "archive_phase0_input_missing")
    adjacent = manifest.get("archivedEtfInputs", [])
    _require(isinstance(adjacent, list) and len(adjacent) <= 1)
    for ref in adjacent:
        _require(isinstance(ref, dict) and set(ref) == {"sourceId", "tradeDate", "objectKey", "sha256", "revisionId", "normalizedPath"}
            and ref["tradeDate"] == requested[-2])
        archived_source_release_id(ref)
        path = str(_safe_path(ref["normalizedPath"]))
        _require(path.startswith("data/normalized/") and path not in expected and isinstance(ref["sha256"], str) and _SHA.fullmatch(ref["sha256"]))
        expected[path] = (ref["objectKey"], "adjacent", ref["sha256"], "etf_scale")
    files = recipe["files"]
    _require(isinstance(files, list) and len(files) == len(expected) <= MAX_FILES)
    seen, total = set(), 0
    for ref in files:
        _require(isinstance(ref, dict) and set(ref) == {"path", "objectKey", "sha256", "bytes"})
        path, key = ref["path"], ref["objectKey"]
        _require(isinstance(path, str) and path in expected and path not in seen and isinstance(key, str))
        seen.add(path)
        prefix, kind, raw_sha, dataset = expected[path]
        if kind in {"raw", "metadata", "adjacent"}:
            _require(key == prefix)
            if kind in {"raw", "adjacent"}:
                _require(ref["sha256"] == raw_sha)
        else:
            digest = r"[a-f0-9]{64}" if dataset == "etf_scale" else re.escape(raw_sha)
            _require(re.fullmatch(re.escape(prefix)+digest+"/"+kind+r"\.csv"+(r"\.gz" if dataset == "etf_pcf" else ""), key))
        _require(isinstance(ref["sha256"], str) and _SHA.fullmatch(ref["sha256"])
            and type(ref["bytes"]) is int and 0 <= ref["bytes"] <= MAX_OBJECT_BYTES)
        total += ref["bytes"]
    _require(total <= MAX_TOTAL_BYTES)
    return manifest


class ArchivedOfficialMarketAdapters:
    """Same collector protocol; missing data is an error, never a live fetch."""

    def __init__(self, recipe, *, target, root, fetch):
        manifest = validate_recipe(recipe, target)
        _require(not Path(root).is_symlink(), "archive_phase0_destination_invalid")
        root = Path(root).resolve()
        _require(not root.is_symlink() and (not root.exists() or root.is_dir()), "archive_phase0_destination_invalid")
        root.mkdir(parents=True, exist_ok=True)
        cache = root / ".archive-wire"
        _require(not cache.is_symlink(), "archive_phase0_destination_invalid")
        cache.mkdir(exist_ok=True)

        def persist(path, data):
            _require(not path.is_symlink() and path.resolve().is_relative_to(root), "archive_phase0_destination_invalid")
            if path.exists():
                _require(path.is_file() and path.read_bytes() == data, "archive_phase0_local_conflict")
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".archive-pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)

        total = 0
        for ref in recipe["files"]:
            cached = cache / ref["sha256"]
            _require(not cached.is_symlink(), "archive_phase0_destination_invalid")
            data = cached.read_bytes() if cached.is_file() else fetch(ref)
            _require(isinstance(data, bytes) and len(data) == ref["bytes"]
                and hashlib.sha256(data).hexdigest() == ref["sha256"], "archive_phase0_object_mismatch")
            persist(cached, data)
            if ref["objectKey"].endswith(".csv.gz"):
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                        data = stream.read(MAX_OBJECT_BYTES+1)
                except (OSError, EOFError, zlib.error) as exc:
                    raise ValueError("archive_phase0_expansion_invalid") from exc
            total += len(data)
            _require(len(data) <= MAX_OBJECT_BYTES and total <= MAX_TOTAL_BYTES, "archive_phase0_expansion_invalid")
            persist(root / ref["path"], data)
        # Reparse raw evidence through the same lineage/mapping validators as
        # a live collection. A body hash alone does not prove business values.
        findings = validate_artifact_lineage(manifest, root) + validate_raw_to_normalized_mapping(manifest, root)
        _require(not any(f.status == "fail" for f in findings), "archive_phase0_lineage_invalid")
        self.root, self.manifest, self.records = root, manifest, {}
        for item in manifest["artifacts"]:
            paths = [root / p for p in item["normalized_path"].split(";")]
            frames = []
            for path in paths:
                try:
                    frames.append(pd.read_csv(path, dtype={"instrument_code": str}))
                except pd.errors.EmptyDataError:
                    frames.append(pd.DataFrame())
            artifact = FetchArtifact(**{**item, "source": SourceSpec(**item["source"]),
                "raw_path": str(root/item["raw_path"]), "metadata_path": str(root/item["metadata_path"]),
                "normalized_path": ";".join(str(p) for p in paths)})
            self.records[(artifact.source.source_id, artifact.source.dataset, artifact.requested_date)] = (frames, artifact)

    def _get(self, source, dataset, day):
        try:
            frames, artifact = self.records[(source, dataset, day)]
        except KeyError as exc:
            raise ValueError("archive_phase0_input_missing") from exc
        return (*[frame.copy(deep=True) for frame in frames], artifact)

    def fetch_sse_etf(self, day): return self._get("sse", "etf_scale", day)
    def _optional(self, source, dataset, day):
        return self._get(source, dataset, day) if (source, dataset, day) in self.records else None

    def fetch_sse_etf_nav(self, day, dates): return self._optional("sse", "etf_nav", day)
    def fetch_sse_etf_pcf(self, codes, day, **kwargs): return self._optional("sse", "etf_pcf", day)
    def fetch_szse_etf_current(self, dates): return self._get("szse", "etf_scale", dates[-1])
    def fetch_sse_margin(self, start, end): return self._get("sse", "margin_market_daily", f"{start}_{end}")
    def fetch_sse_margin_security(self, day): return self._optional("sse", "margin_security_daily", day)
    def fetch_szse_margin(self, day): return self._get("szse", "margin_market_daily", day)
    def fetch_bse_margin(self, day): return self._get("bse", "margin_market_daily", day)
    def fetch_hkex_connect(self, day): return self._get("hkex", "stock_connect_daily", day)
