"""Execute one approved recovery plan against official sources.

This is the bounded, date-major runner for the recovery workflow.  It is
deliberately separate from the ordinary three-date collector: every output is
written below the approved isolated recovery root, every source date is
derived from an official calendar, and a missing/ambiguous input fails closed.
The runner never changes an active read-model pointer; the publish workflow
owns candidate verification and activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

from trading_desk.adapters.official import (
    SZSE_ETF_METADATA_URL,
    SZSE_ETF_PAGE_URL,
    _szse_etf_dates,
)
from trading_desk.dashboard import build_dashboard_snapshot, discover_recent_trading_dates
from trading_desk.etf_stock_flow import build_stock_flow, build_stock_flow_catalog
from trading_desk.etf_flow_state import materialize_dashboard_snapshot
from trading_desk.history_bundle import build_history_bundle_from_files
from trading_desk.history_publish import publish_history_bundle
from trading_desk.http import HttpSettings, build_session
from trading_desk.phase0 import collect_phase0
from trading_desk.recovery_execution import (
    RecoveryContractError,
    RecoveryScope,
    assert_recovery_output_path,
    parse_run_key,
    validate_recovery_plan,
)


class LiveRecoveryError(RuntimeError):
    """A live recovery stopped before it could produce a safe artifact."""

    def __init__(self, code: str, detail: str = "", *, status: str = "BLOCKED_QUALITY") -> None:
        self.code = code
        self.status = status
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


# Keep the runner executable both as ``python -m`` and as the workflow's
# ``python scripts/run_live_recovery.py`` entry point.  Importing the sibling
# script package is not reliable in the latter mode, while the URL is a stable
# official referer used by both recovery paths.
SZSE_CURL_PAGE_URL = "https://fund.szse.cn/marketdata/fundslist/index.html"


@dataclass
class BudgetedRateLimitedSession:
    """A requests-compatible session with a small, observable request budget.

    The live runner supplies an adapter with automatic retries disabled.  A
    request is therefore counted exactly once, while this wrapper owns the
    pacing and fail-closed retry decisions.  This keeps the recovery budget
    deterministic while preventing concurrent requests to an exchange host.
    """

    session: requests.Session
    maximum: int
    min_interval_seconds: float = 2.0
    request_count: int = 0
    last_by_host: dict[str, float] | None = None
    szse_curl: bool = False

    def __post_init__(self) -> None:
        if self.maximum <= 0:
            raise ValueError("max_requests must be positive")
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be non-negative")
        if self.last_by_host is None:
            self.last_by_host = {}

    @property
    def request_timeout(self) -> tuple[float, float]:
        return getattr(self.session, "request_timeout", (8.0, 25.0))

    @request_timeout.setter
    def request_timeout(self, value: tuple[float, float]) -> None:
        setattr(self.session, "request_timeout", value)

    @property
    def headers(self) -> Any:
        return self.session.headers

    def _reserve_request(self, url: str) -> None:
        if self.request_count >= self.maximum:
            raise LiveRecoveryError(
                "live_recovery_request_budget_exceeded",
                f"maximum={self.maximum}",
                status="FAILED_RETRYABLE",
            )
        parsed = urlparse(url)
        host = (parsed.hostname or "unknown").lower()
        now = time.monotonic()
        assert self.last_by_host is not None  # initialized by __post_init__
        previous = self.last_by_host.get(host)
        if previous is not None:
            delay = self.min_interval_seconds - (now - previous)
            if delay > 0:
                time.sleep(delay)
        self.request_count += 1
        self.last_by_host[host] = time.monotonic()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._reserve_request(url)
        return self.session.request(method, url, **kwargs)

    def _request_szse_with_curl(self, url: str, **kwargs: Any) -> requests.Response:
        """Fetch an official SZSE response through the bounded curl path.

        GitHub-hosted runners have intermittently had their Python TLS
        connection reset by ``fund.szse.cn``.  The historical recovery helper
        already uses curl with a conservative timeout and no retries.  Reuse
        that transport for the small current-metadata probe (and any current
        workbook request) while keeping the outer request budget and per-host
        pacing authoritative.  A resolver override is optional because the
        exchange's public address can change; when supplied it is validated by
        the helper's ``FLOW_RECOVERY_SZSE_RESOLVE`` contract.
        """

        params = kwargs.get("params")
        headers = kwargs.get("headers")
        resolve = os.environ.get("FLOW_RECOVERY_SZSE_RESOLVE", "").strip()
        if resolve and not re.fullmatch(r"fund\.szse\.cn:443:(?:\d{1,3}\.){3}\d{1,3}", resolve):
            raise LiveRecoveryError(
                "live_recovery_source_unavailable",
                "invalid_szse_resolver",
                status="BLOCKED_QUALITY",
            )
        self._reserve_request(url)
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--retry",
            "0",
            "--max-time",
            "90",
            "-A",
            str(headers.get("User-Agent") if isinstance(headers, dict) and headers.get("User-Agent") else "Mozilla/5.0"),
            "-H",
            f"Referer: {headers.get('Referer') if isinstance(headers, dict) and headers.get('Referer') else SZSE_CURL_PAGE_URL}",
            "--get",
        ]
        if resolve:
            command[8:8] = ["--resolve", resolve]
        if isinstance(headers, dict) and headers.get("Accept"):
            command.extend(["-H", f"Accept: {headers['Accept']}"])
        if isinstance(params, dict):
            for key, value in params.items():
                values = value if isinstance(value, (list, tuple)) else (value,)
                for item in values:
                    command.extend(["--data-urlencode", f"{key}={item}"])
        command.append(url)
        try:
            result = subprocess.run(
                command,
                cwd=None,
                check=False,
                capture_output=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired as exc:
            raise LiveRecoveryError(
                "live_recovery_source_unavailable",
                "szse_curl_timeout",
                status="FAILED_RETRYABLE",
            ) from exc
        except OSError as exc:
            raise LiveRecoveryError(
                "live_recovery_source_unavailable",
                "szse_curl_unavailable",
                status="FAILED_RETRYABLE",
            ) from exc
        if result.returncode != 0:
            raise LiveRecoveryError(
                "live_recovery_source_unavailable",
                f"szse_curl_exit={result.returncode}",
                status="FAILED_RETRYABLE",
            )
        content = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout).encode("utf-8")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/octet-stream"
        response._content = content
        response.encoding = "utf-8"
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        parsed = urlparse(url)
        if self.szse_curl and parsed.hostname and parsed.hostname.lower() == "fund.szse.cn":
            response = self._request_szse_with_curl(url, **kwargs)
        else:
            response = self._request("GET", url, **kwargs)
        if getattr(response, "status_code", None) in {403, 429}:
            # Do not let the generic official adapter turn a WAF/rate-limit
            # response into an ordinary quality warning and continue with the
            # remaining datasets.  Recovery must stop this invocation before
            # another source request is issued.
            raise LiveRecoveryError(
                "live_recovery_source_unavailable",
                f"HTTP {response.status_code}",
                status="FAILED_RETRYABLE",
            )
        return response

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        response = self._request("POST", url, **kwargs)
        if getattr(response, "status_code", None) in {403, 429}:
            raise LiveRecoveryError(
                "live_recovery_source_unavailable",
                f"HTTP {response.status_code}",
                status="FAILED_RETRYABLE",
            )
        return response


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LiveRecoveryError("live_recovery_auth_missing", name, status="BLOCKED_QUALITY")
    return value


def _auth_headers() -> dict[str, str]:
    # Keep values in memory only; never include this dictionary in evidence or
    # an exception message.
    return {
        "Authorization": f"Bearer {_required_env('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": _required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": _required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
    }


def _safe_root(workspace_root: Path, value: str, field: str) -> Path:
    assert_recovery_output_path(value)
    root = (workspace_root / value).resolve()
    workspace = workspace_root.resolve()
    try:
        root.relative_to(workspace)
    except ValueError as exc:
        raise LiveRecoveryError("live_recovery_root_escape", field) from exc
    if not (value == ".recovery" or value.startswith(".recovery/") or value == "recovery" or value.startswith("recovery/")):
        raise LiveRecoveryError("live_recovery_root_not_isolated", field)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveRecoveryError("live_recovery_plan_read_failed", str(path)) from exc
    if not isinstance(value, dict):
        raise LiveRecoveryError("live_recovery_json_object_required", str(path))
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_json(source: Path, destination: Path) -> None:
    _write_json(destination, _read_json(source))


def _post_child_request_reserve(manifest: dict[str, Any]) -> int:
    """Reserve worst-case requests that follow the stock-flow child.

    The child process has its own retry loop, but the parent owns one global
    recovery allocation. Reserving the two compact history reads plus three
    attempts for every final history-import chunk prevents the child from
    consuming the entire allowance and leaving the final archive publish to
    overspend or fail halfway through.
    """

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise LiveRecoveryError("live_recovery_history_artifacts_missing")
    return 2 + (len(artifacts) + 1) * 3


def _szse_history_request_budget(scope: RecoveryScope) -> int:
    """Read the explicit SZSE historical-source allocation from the plan.

    Historical SZSE recovery is a per-fund request fan-out.  It must not
    silently consume the generic recovery allowance because doing so could
    starve the publish checkpoint or exceed the account's measured budget.
    An approved plan therefore carries a dedicated, immutable allocation.
    """

    manifest = scope.budget_manifest
    if not isinstance(manifest, dict):
        raise LiveRecoveryError(
            "live_recovery_szse_history_budget_missing",
            "budgetManifest must include szseHistoryMaxRequests",
            status="BLOCKED_QUALITY",
        )
    value = manifest.get("szseHistoryMaxRequests")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LiveRecoveryError(
            "live_recovery_szse_history_budget_missing",
            "budgetManifest.szseHistoryMaxRequests must be a positive integer",
            status="BLOCKED_QUALITY",
        )
    if value >= scope.max_requests:
        raise LiveRecoveryError(
            "live_recovery_szse_history_budget_invalid",
            "historical allocation must leave room for the collector and publish checkpoints",
            status="BLOCKED_QUALITY",
        )
    return value


def _invoke_szse_etf_history(
    *,
    scope: RecoveryScope,
    dates: list[str],
    artifact_root: Path,
    workspace_root: Path,
    max_requests: int,
) -> tuple[Path, int]:
    """Fetch exact-date SZSE scale evidence into the isolated recovery root."""

    if not dates or max_requests <= 0:
        raise LiveRecoveryError(
            "live_recovery_szse_history_budget_invalid",
            "dates and max_requests are required",
            status="BLOCKED_QUALITY",
        )
    output_root = artifact_root / "szse-etf-history"
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        min_interval = float(os.environ.get("FLOW_RECOVERY_SZSE_HISTORY_MIN_INTERVAL_SECONDS", "0.5"))
        workers = int(os.environ.get("FLOW_RECOVERY_SZSE_HISTORY_WORKERS", "4"))
    except ValueError as exc:
        raise LiveRecoveryError(
            "live_recovery_szse_history_limits_invalid",
            "history workers and interval must be numeric",
            status="BLOCKED_QUALITY",
        ) from exc
    if not math.isfinite(min_interval) or min_interval < 0.5:
        raise LiveRecoveryError(
            "live_recovery_szse_history_interval_too_short",
            "minimum is 0.5 seconds",
            status="BLOCKED_QUALITY",
        )
    if workers < 1 or workers > 8:
        raise LiveRecoveryError(
            "live_recovery_szse_history_workers_invalid",
            "workers must be between 1 and 8",
            status="BLOCKED_QUALITY",
        )

    command = [
        sys.executable,
        str(workspace_root / "scripts/recover_szse_etf_history.py"),
        "--dates",
        *dates,
        "--output-root",
        str(output_root),
        "--workers",
        str(workers),
        "--max-requests",
        str(max_requests),
        "--min-interval",
        str(min_interval),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{workspace_root / 'src'}:{workspace_root}"
    try:
        subprocess.run(
            command,
            cwd=workspace_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        # Do not echo child output: a request error could contain query
        # parameters or credentials supplied by the runner environment.
        raise LiveRecoveryError(
            "live_recovery_szse_history_fetch_failed",
            f"exit={exc.returncode}",
            status="FAILED_RETRYABLE",
        ) from exc

    manifest_path = output_root / "manifest.json"
    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise LiveRecoveryError(
            "live_recovery_szse_history_manifest_invalid",
            "history helper did not produce a valid manifest",
            status="BLOCKED_QUALITY",
        ) from exc
    manifest_dates = manifest.get("dates")
    artifacts = manifest.get("artifacts")
    request_count = manifest.get("requestCount")
    artifact_dates = (
        [
            artifact.get("requested_date") if isinstance(artifact, dict) else None
            for artifact in artifacts
        ]
        if isinstance(artifacts, list)
        else []
    )
    artifact_dates_are_strings = all(isinstance(value, str) for value in artifact_dates)
    if (
        manifest_dates != dates
        or not isinstance(artifacts, list)
        or len(artifacts) != len(dates)
        or not artifact_dates_are_strings
        or (
            artifact_dates_are_strings
            and (
                sorted(artifact_dates) != sorted(dates)
                or len(set(artifact_dates)) != len(dates)
            )
        )
        or isinstance(request_count, bool)
        or not isinstance(request_count, int)
        or request_count < 0
        or request_count > max_requests
    ):
        raise LiveRecoveryError(
            "live_recovery_szse_history_manifest_invalid",
            "history dates, artifact count or request budget do not match the approved scope",
            status="BLOCKED_QUALITY",
        )
    for artifact in artifacts:
        source = artifact.get("source") if isinstance(artifact, dict) else None
        if (
            not isinstance(artifact, dict)
            or artifact.get("requested_date") not in dates
            or not isinstance(source, dict)
            or source.get("source_id") != "szse"
            or source.get("dataset") != "etf_scale"
            or source.get("date_semantics") != "source_historical_scale_size_date"
        ):
            raise LiveRecoveryError(
                "live_recovery_szse_history_manifest_invalid",
                "history artifact source or date semantics are invalid",
                status="BLOCKED_QUALITY",
            )
    return output_root, request_count


def _get_json(
    session: Any,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> tuple[dict[str, Any], requests.Response]:
    try:
        response = session.get(url, params=params, headers=headers, timeout=(10, 90))
        response.raise_for_status()
        value = response.json()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            code = "live_recovery_remote_not_found"
        elif status in {403, 429}:
            code = "live_recovery_source_unavailable"
        else:
            code = "live_recovery_remote_read_failed"
        raise LiveRecoveryError(code, f"HTTP {status or 'unknown'}", status="FAILED_RETRYABLE") from exc
    except (requests.RequestException, ValueError) as exc:
        raise LiveRecoveryError("live_recovery_remote_read_failed", type(exc).__name__, status="FAILED_RETRYABLE") from exc
    if not isinstance(value, dict):
        raise LiveRecoveryError("live_recovery_remote_payload_invalid", url)
    return value, response


def _state_dates(state: dict[str, Any], target: str) -> list[str]:
    raw = state.get("dates")
    if not isinstance(raw, list):
        raise LiveRecoveryError("live_recovery_state_dates_missing")
    values = sorted({str(value) for value in raw if isinstance(value, str) and len(value) == 10})
    if target not in values:
        raise LiveRecoveryError("live_recovery_state_missing_target", target, status="FAILED_RETRYABLE")
    return values


def _assert_confirmed_snapshot(snapshot: dict[str, Any], target: str) -> None:
    publication = snapshot.get("publication")
    if not isinstance(publication, dict) or publication.get("status") != "confirmed":
        raise LiveRecoveryError("live_recovery_quality_gate", "snapshot is not confirmed")
    if str(snapshot.get("asOf") or "") != target:
        raise LiveRecoveryError("live_recovery_date_anchor_mismatch", f"expected={target}")


def _assert_szse_current_snapshot_is_adjacent(
    *,
    manifest: dict[str, Any],
    target: str,
    session: Any,
) -> None:
    """Reject a moving SZSE list when it cannot prove the target date.

    ``fetch_szse_etf_current`` is intentionally a *current* snapshot adapter.
    It maps the list page's disclosure date to the preceding known trading
    date, which is correct for a normal forward collection window but unsafe
    for a historical recovery: a September workbook would otherwise be
    relabelled as an August target.  A recovery may use this adapter only when
    the page date's immediately preceding official trading day is the target.
    Older targets must use the retained per-fund historical endpoint and are
    rejected before any derived snapshot can be published.
    """

    page_value = manifest.get("szse_etf_page_date")
    page_date = str(page_value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", page_date):
        raise LiveRecoveryError(
            "live_recovery_historical_szse_source_required",
            f"target={target}, page_date=missing",
            status="BLOCKED_QUALITY",
        )
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target):
        raise LiveRecoveryError("live_recovery_date_anchor_mismatch", f"expected={target}")
    if page_date <= target:
        raise LiveRecoveryError(
            "live_recovery_historical_szse_source_required",
            f"target={target}, page_date={page_date}",
            status="BLOCKED_QUALITY",
        )

    try:
        preceding_dates = discover_recent_trading_dates(
            end_date=date.fromisoformat(page_date) - timedelta(days=1),
            count=2,
            session=session,
        )
    except Exception as exc:
        if getattr(exc, "code", None) in {
            "live_recovery_request_budget_exceeded",
            "live_recovery_source_unavailable",
        }:
            raise
        raise LiveRecoveryError(
            "live_recovery_historical_szse_calendar_unavailable",
            type(exc).__name__,
            status="FAILED_RETRYABLE",
        ) from exc

    if not preceding_dates or preceding_dates[-1] != target:
        detail = ",".join(str(value) for value in preceding_dates[-2:]) or "missing"
        raise LiveRecoveryError(
            "live_recovery_historical_szse_source_required",
            f"target={target}, page_date={page_date}, preceding={detail}",
            status="BLOCKED_QUALITY",
        )


def _current_szse_etf_matches_target(
    *, session: BudgetedRateLimitedSession, target: str, context_dates: list[str]
) -> bool:
    """Use the moving workbook only when the official page proves this date.

    The current workbook needs two source requests instead of a per-fund
    history sweep.  Older dates still use exact-date retained history.  This
    probe is counted by the same bounded session as the rest of the collector.
    """

    response = session.get(
        SZSE_ETF_METADATA_URL,
        params={
            "SHOWTYPE": "JSON",
            "CATALOGID": "1000_lf",
            "TABKEY": "tab1",
            "tab1PAGENO": "1",
            "tab1PAGESIZE": "1",
        },
        headers={"Referer": SZSE_ETF_PAGE_URL, "User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    try:
        page_date, effective_date, _header, displayed_date = _szse_etf_dates(
            response.json(), context_dates
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise LiveRecoveryError(
            "live_recovery_szse_current_metadata_invalid",
            type(exc).__name__,
            status="BLOCKED_QUALITY",
        ) from exc
    if effective_date != target:
        return False
    try:
        _assert_szse_current_snapshot_is_adjacent(
            manifest={"szse_etf_page_date": page_date}, target=target, session=session
        )
    except LiveRecoveryError as exc:
        if exc.code == "live_recovery_historical_szse_source_required":
            return False
        raise
    # The display label may be the page day or the underlying share day; a
    # third date cannot support this recovery's source-date claim.
    if displayed_date is not None and displayed_date not in {target, page_date}:
        raise LiveRecoveryError(
            "live_recovery_szse_current_date_mismatch",
            f"target={target}, page_date={page_date}, displayed={displayed_date}",
            status="BLOCKED_QUALITY",
        )
    return True


def _assert_history_checkpoint(response: dict[str, Any], target: str) -> None:
    # HTTP 200 can also mean waiting_sources / archived_pending. Neither is
    # evidence that the target's normalized history checkpoint is readable.
    if response.get("status") != "archived" or response.get("asOf") != target:
        pending_sources = (
            response.get("asOf") == target
            and response.get("status") in {"waiting_sources", "archived_pending"}
            and not response.get("qualityIssues")
            and response.get("reason") != "derived_etf_quality_failed"
        )
        raise LiveRecoveryError(
            "live_recovery_history_checkpoint_incomplete",
            f"target={target}, status={response.get('status')}, asOf={response.get('asOf')}",
            status="FAILED_RETRYABLE" if pending_sources else "BLOCKED_QUALITY",
        )


def _invoke_stock_flow_history(
    *,
    scope: RecoveryScope,
    state: dict[str, Any],
    target: str,
    artifact_root: Path,
    workspace_root: Path,
    remaining_requests: int,
    auth_env: dict[str, str],
) -> tuple[Path, Path, Path, int]:
    dates = [value for value in _state_dates(state, target) if value <= target]
    # The builder needs one previous share date for every published partition.
    dates = dates[-22:]
    if len(dates) < 2:
        raise LiveRecoveryError("live_recovery_stock_flow_anchor_missing", target)
    dates_path = artifact_root / "stock-flow-dates.txt"
    dates_path.write_text("\n".join(dates) + "\n", encoding="utf-8")
    inputs_dir = artifact_root / "stock-flow-inputs"
    report_path = artifact_root / "stock-flow-fetch-report.json"
    inventory_path = artifact_root / "pcf-inventory.json"
    if remaining_requests <= 0:
        raise LiveRecoveryError(
            "live_recovery_request_budget_exceeded",
            "no request budget remains for stock-flow inputs",
            status="FAILED_RETRYABLE",
        )
    command = [
        sys.executable,
        str(workspace_root / "scripts/fetch_stock_flow_history_inputs.py"),
        "--base-url",
        os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"),
        "--dates",
        str(dates_path),
        "--as-of",
        target,
        "--output-dir",
        str(inputs_dir),
        "--report",
        str(report_path),
        "--inventory",
        str(inventory_path),
        "--max-requests",
        str(remaining_requests),
        "--require-current",
    ]
    env = os.environ.copy()
    env.update(auth_env)
    env["PYTHONPATH"] = f"{workspace_root / 'src'}:{workspace_root}"
    try:
        subprocess.run(command, cwd=workspace_root, env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        # The child report is retained in the isolated root.  Include only a
        # stable code in the raised error; never echo command output because it
        # could contain a URL with request parameters or an accidental secret.
        if exc.returncode == 42:
            code, status = "live_recovery_stock_flow_lineage_mismatch", "BLOCKED_QUALITY"
        elif exc.returncode == 43:
            code, status = "live_recovery_request_budget_exceeded", "FAILED_RETRYABLE"
        elif exc.returncode == 44:
            code, status = "live_recovery_source_unavailable", "FAILED_RETRYABLE"
        else:
            code, status = "live_recovery_stock_flow_fetch_failed", "FAILED_RETRYABLE"
        raise LiveRecoveryError(code, f"exit={exc.returncode}", status=status) from exc
    report = _read_json(report_path)
    request_count = int(report.get("requestCount") or 0)
    if request_count > remaining_requests:
        raise LiveRecoveryError("live_recovery_request_budget_exceeded", "child report exceeded allocation", status="FAILED_RETRYABLE")
    partitions_dir = artifact_root / "stock-flow-partitions"
    history_path = artifact_root / "stock-flow-history.json"
    try:
        subprocess.run(
            [
                sys.executable,
                str(workspace_root / "scripts/build_stock_flow_history.py"),
                "--shares",
                str(inputs_dir / "shares.csv"),
                "--pcf",
                str(inputs_dir / "pcf.csv"),
                "--dates",
                str(dates_path),
                "--as-of",
                target,
                "--output-dir",
                str(partitions_dir),
                "--output",
                str(history_path),
                "--max-partitions",
                "21",
                "--lineage-report",
                str(report_path),
            ],
            cwd=workspace_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise LiveRecoveryError("live_recovery_stock_flow_quality_failed", f"exit={exc.returncode}") from exc
    history = _read_json(history_path)
    if target not in (history.get("availableDates") or []):
        raise LiveRecoveryError("live_recovery_stock_flow_current_partition_missing", target)
    status_path = artifact_root / "stock-flow-history-status.json"
    _write_json(
        status_path,
        {
            "schemaVersion": 1,
            "status": "READY",
            "asOf": target,
            "availableDates": history.get("availableDates") or [],
            "missingDates": history.get("missingDates") or [],
            "requestCount": request_count,
        },
    )
    return history_path, status_path, dates_path, request_count


def _fetch_market_history(
    *,
    base_url: str,
    target: str,
    output_dir: Path,
    headers: dict[str, str],
    session: Any,
) -> None:
    history, _ = _get_json(
        session,
        urljoin(base_url.rstrip("/") + "/", "api/v2/market-history"),
        headers={key: value for key, value in headers.items() if key.startswith("CF-")},
        params={"asOf": target},
    )
    rows = history.get("rows")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("trade_date"), str)
        or row["trade_date"] > target
        for row in rows
    ):
        raise LiveRecoveryError("live_recovery_market_history_invalid", target)
    # A compact history response that only contains prior rows is not a
    # valid checkpoint for this date.  Requiring the target itself prevents a
    # successful-looking recovery from publishing an older as-of date.
    if not any(
        isinstance(row, dict) and row.get("trade_date") == target
        for row in rows
    ):
        raise LiveRecoveryError("live_recovery_market_history_target_missing", target)
    _write_json(output_dir / "market-history.json", history)
    previous, _ = _get_json(
        session,
        urljoin(base_url.rstrip("/") + "/", "api/v2/releases/latest"),
        headers={key: value for key, value in headers.items() if key.startswith("CF-")},
    )
    _write_json(output_dir / "previous-release.json", previous)


def run_live_recovery(
    *,
    plan: dict[str, Any],
    expected_plan_hash: str | None,
    attempt: int,
    producer_build_sha: str,
    workspace_root: Path,
    base_url: str,
) -> dict[str, Any]:
    """Run the approved live recovery and return isolated evidence metadata."""

    scope = validate_recovery_plan(plan, expected_plan_hash=expected_plan_hash)
    if attempt < 0:
        raise LiveRecoveryError("live_recovery_attempt_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", producer_build_sha or ""):
        raise LiveRecoveryError("live_recovery_producer_build_sha_invalid")
    deadline = datetime.fromisoformat(scope.deadline.replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > deadline:
        raise LiveRecoveryError("live_recovery_deadline_expired")
    parsed_run_key = parse_run_key(scope.run_key)
    if len(scope.data_dates) != 1:
        raise LiveRecoveryError(
            "live_recovery_multi_date_requires_date_major_dispatch",
            "one live runner invocation may process exactly one approved date",
            status="BLOCKED_QUALITY",
        )
    target = scope.data_dates[0]
    # The plan hash binds this single output date to the original window.
    # Rewriting a September reconcile runKey to an August dataDate would
    # bypass the original window's lease and recovery audit trail.

    artifact_root = _safe_root(workspace_root, scope.artifact_root, "artifact_root")
    state_root = _safe_root(workspace_root, scope.state_dir, "state_dir")
    source_bundle = artifact_root / "source-bundle"
    source_bundle.mkdir(parents=True, exist_ok=True)
    data_root = artifact_root / "data"
    report_root = artifact_root / "reports" / "phase0"
    data_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)

    auth = _auth_headers()
    auth_env = {
        "FLOW_INGEST_API_KEY": os.environ["FLOW_INGEST_API_KEY"],
        "FLOW_CF_ACCESS_CLIENT_ID": os.environ["FLOW_CF_ACCESS_CLIENT_ID"],
        "FLOW_CF_ACCESS_CLIENT_SECRET": os.environ["FLOW_CF_ACCESS_CLIENT_SECRET"],
    }
    szse_history_budget = _szse_history_request_budget(scope)
    min_interval = float(os.environ.get("FLOW_RECOVERY_MIN_INTERVAL_SECONDS", "2"))
    if min_interval < 2:
        raise LiveRecoveryError("live_recovery_source_interval_too_short", "minimum is 2 seconds")
    source_session = BudgetedRateLimitedSession(
        # Automatic urllib retries would hide additional official requests
        # from the outer budget and could turn a 403/429 into a burst.  The
        # bounded runner owns retry/stop decisions explicitly.
        build_session(HttpSettings(total_retries=0)),
        maximum=scope.max_requests,
        min_interval_seconds=min_interval,
        szse_curl=True,
    )
    context_dates = discover_recent_trading_dates(
        end_date=date.fromisoformat(target),
        count=3,
        session=source_session,
    )
    if context_dates[-1] != target or not set(scope.data_dates).issubset(context_dates):
        raise LiveRecoveryError(
            "live_recovery_official_calendar_mismatch",
            f"target={target}, context={','.join(context_dates)}",
        )

    required = set(scope.required_stages)
    stock_flow_required = bool(required.intersection({"etf_penetration", "stock_flow"}))
    current_szse_matches = _current_szse_etf_matches_target(
        session=source_session, target=target, context_dates=context_dates
    )
    allocated_szse_budget = 0
    szse_history_root: Path | None = None
    szse_history_request_count = 0
    if not current_szse_matches:
        allocated_szse_budget = szse_history_budget
        parent_request_budget = scope.max_requests - allocated_szse_budget
        if parent_request_budget <= source_session.request_count:
            raise LiveRecoveryError(
                "live_recovery_request_budget_exceeded",
                "historical SZSE allocation leaves no room for the collector",
                status="FAILED_RETRYABLE",
            )
        source_session.maximum = parent_request_budget
        szse_history_root, szse_history_request_count = _invoke_szse_etf_history(
            scope=scope,
            dates=context_dates,
            artifact_root=artifact_root,
            workspace_root=workspace_root,
            max_requests=szse_history_budget,
        )
    collection = collect_phase0(
        dates=context_dates,
        data_root=data_root,
        report_root=report_root,
        workspace_root=workspace_root,
        collection_window="reconcile",
        # Penetration recovery is archive-first.  Do not spend live official
        # PCF requests for the same date before the bounded archive fetch has
        # had a chance to reuse the verified PCF objects.  A dashboard-only
        # scope may retain the ordinary small PCF enrichment.
        pcf_max_codes=(0 if stock_flow_required else min(16, max(1, scope.max_requests // 4))),
        session=source_session,
        pcf_max_workers=1,
        szse_etf_history_root=szse_history_root,
    )
    manifest = collection["manifest"]
    quality = collection["quality"]
    if current_szse_matches:
        if manifest.get("szse_etf_trade_date") != target:
            raise LiveRecoveryError(
                "live_recovery_szse_current_date_mismatch",
                f"expected={target}, observed={manifest.get('szse_etf_trade_date')}",
                status="BLOCKED_QUALITY",
            )
        _assert_szse_current_snapshot_is_adjacent(
            manifest=manifest, target=target, session=source_session
        )
    run_id = str(manifest.get("run_id") or "")
    manifest_path = report_root / run_id / "manifest.json"
    quality_path = report_root / run_id / "quality-summary.json"
    if int((quality.get("counts") or {}).get("fail", 0) or 0) > 0:
        raise LiveRecoveryError("live_recovery_quality_gate", "phase0 validation failed")
    base_snapshot_path = source_bundle / "dashboard-snapshot-base.json"
    stock_catalog_path = source_bundle / "stock-flow-catalog.json"
    base_snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=workspace_root,
        stock_catalog_path=stock_catalog_path,
    )
    _assert_confirmed_snapshot(base_snapshot, target)
    _write_json(base_snapshot_path, base_snapshot)

    state_url = urljoin(base_url.rstrip("/") + "/", "api/v1/admin/etf-state")
    # Use the same bounded transport for source reads and history-import POSTs
    # so the evidence accounts for every Worker operation and the per-host
    # interval also applies to the archive endpoint.
    state_session = source_session
    state: dict[str, Any]
    state_before_path = state_root / "etf-flow-state-before.json"
    try:
        state, _ = _get_json(state_session, state_url, headers=auth)
    except LiveRecoveryError as exc:
        if exc.code != "live_recovery_remote_not_found":
            raise
        state = {"dates": []}
    _write_json(state_before_path, state)
    before_dates = state.get("dates") if isinstance(state.get("dates"), list) else []
    history_publications: list[dict[str, Any]] = []
    if target not in before_dates:
        bundle = build_history_bundle_from_files(
            snapshot_path=base_snapshot_path,
            manifest_path=manifest_path,
            workspace_root=workspace_root,
        )
        response = publish_history_bundle(
            url=urljoin(base_url.rstrip("/") + "/", "api/v1/admin/history-import"),
            bundle=bundle,
            ingest_key=auth_env["FLOW_INGEST_API_KEY"],
            access_client_id=auth_env["FLOW_CF_ACCESS_CLIENT_ID"],
            access_client_secret=auth_env["FLOW_CF_ACCESS_CLIENT_SECRET"],
            session=source_session,
            max_attempts=3,
        )
        history_publications.append(response)
        _assert_history_checkpoint(response, target)
        state, _ = _get_json(state_session, state_url, headers=auth)
    _state_dates(state, target)
    _write_json(state_root / "etf-flow-state.json", state)

    history_path: Path | None = None
    history_status_path: Path | None = None
    dates_path: Path | None = None
    stock_flow_report_path: Path | None = None
    pcf_inventory_path: Path | None = None
    stock_flow_request_count = 0
    post_child_request_reserve = 0
    if stock_flow_required:
        post_child_request_reserve = _post_child_request_reserve(manifest)
        remaining_for_child = (
            scope.max_requests
            - allocated_szse_budget
            - source_session.request_count
            - post_child_request_reserve
        )
        if remaining_for_child <= 0:
            raise LiveRecoveryError(
                "live_recovery_request_budget_exceeded",
                f"reserve={post_child_request_reserve}, used={source_session.request_count}",
                status="FAILED_RETRYABLE",
            )
        history_path, history_status_path, dates_path, stock_flow_request_count = _invoke_stock_flow_history(
            scope=scope,
            state=state,
            target=target,
            artifact_root=artifact_root,
            workspace_root=workspace_root,
            remaining_requests=remaining_for_child,
            auth_env=auth_env,
        )
        stock_flow_report_path = artifact_root / "stock-flow-fetch-report.json"
        pcf_inventory_path = artifact_root / "pcf-inventory.json"
        # The ordinary phase-0 collector intentionally limits its PCF capture
        # for the daily path.  Recovery is archive-first: rebuild the complete
        # current stock-flow/catalog from every exact-date share and PCF row
        # fetched above, then materialize the dashboard from that corrected
        # model.  Never promote the limited base snapshot as a substitute.
        shares_path = artifact_root / "stock-flow-inputs" / "shares.csv"
        pcf_path = artifact_root / "stock-flow-inputs" / "pcf.csv"
        try:
            archive_shares = pd.read_csv(shares_path, dtype=str)
            archive_pcf = pd.read_csv(pcf_path, dtype=str)
            security_margin_frame: pd.DataFrame | None = None
            security_relative = (manifest.get("combined_paths") or {}).get("sse_margin_security")
            if security_relative:
                security_path = (workspace_root / str(security_relative)).resolve()
                try:
                    security_path.relative_to(workspace_root.resolve())
                except ValueError as exc:
                    raise LiveRecoveryError("live_recovery_security_margin_path_escape") from exc
                if security_path.is_file():
                    security_margin_frame = pd.read_csv(security_path, dtype=str)
            rebuilt_flow = build_stock_flow(
                archive_shares,
                archive_pcf,
                as_of=target,
                previous_date=dates_path.read_text(encoding="utf-8").splitlines()[-2],
            )
            rebuilt_catalog = build_stock_flow_catalog(
                archive_shares,
                archive_pcf,
                as_of=target,
                previous_date=dates_path.read_text(encoding="utf-8").splitlines()[-2],
                security_margin_frame=security_margin_frame,
            )
        except LiveRecoveryError:
            raise
        except Exception as exc:
            raise LiveRecoveryError(
                "live_recovery_stock_flow_quality_failed",
                type(exc).__name__,
                status="BLOCKED_QUALITY",
            ) from exc
        _write_json(stock_catalog_path, rebuilt_catalog)
        base_snapshot["etfStockFlow"] = rebuilt_flow
        flow = rebuilt_flow
        coverage = flow.get("coverage") if isinstance(flow, dict) else None
        if not isinstance(flow, dict) or flow.get("available") is not True or not isinstance(coverage, dict) or int(coverage.get("stockCount", 0) or 0) <= 0:
            raise LiveRecoveryError("live_recovery_stock_flow_quality_failed", "current catalog is unavailable")
        for path in (history_path, history_status_path, dates_path):
            if path is not None and path.is_file():
                destination = source_bundle / path.name
                if path.suffix == ".json":
                    _copy_json(path, destination)
                else:
                    destination.write_bytes(path.read_bytes())
        for path in (stock_flow_report_path, pcf_inventory_path):
            if path.is_file():
                _copy_json(path, source_bundle / path.name)
        partitions = artifact_root / "stock-flow-partitions"
        if partitions.is_dir():
            destination = source_bundle / "stock-flow-partitions"
            destination.mkdir(parents=True, exist_ok=True)
            for path in sorted(partitions.glob("*.json")):
                (destination / path.name).write_bytes(path.read_bytes())
    else:
        _write_json(
            source_bundle / "stock-flow-history-status.json",
            {"schemaVersion": 1, "status": "NOT_REQUIRED", "asOf": target},
        )

    final_snapshot = materialize_dashboard_snapshot(base_snapshot, state, as_of=target)
    _assert_confirmed_snapshot(final_snapshot, target)
    _write_json(source_bundle / "dashboard-snapshot.json", final_snapshot)
    _copy_json(base_snapshot_path, source_bundle / "dashboard-snapshot-base.json")

    # Archive the final materialized snapshot as the immutable date checkpoint.
    # The server is idempotent for the same source artifact release IDs, so this
    # finalize is safe when the state already contained the target. It must
    # precede the market-history read: ETF state may contain a date whose
    # independent history checkpoint was never finalized. Requiring that
    # checkpoint before creating it makes recovery impossible in that case.
    final_bundle = build_history_bundle_from_files(
        snapshot_path=source_bundle / "dashboard-snapshot.json",
        manifest_path=manifest_path,
        workspace_root=workspace_root,
    )
    final_response = publish_history_bundle(
        url=urljoin(base_url.rstrip("/") + "/", "api/v1/admin/history-import"),
        bundle=final_bundle,
        ingest_key=auth_env["FLOW_INGEST_API_KEY"],
        access_client_id=auth_env["FLOW_CF_ACCESS_CLIENT_ID"],
        access_client_secret=auth_env["FLOW_CF_ACCESS_CLIENT_SECRET"],
        session=source_session,
        max_attempts=3,
    )
    history_publications.append(final_response)
    _assert_history_checkpoint(final_response, target)
    _fetch_market_history(
        base_url=base_url,
        target=target,
        output_dir=source_bundle,
        headers=auth,
        session=source_session,
    )
    request_count = (
        szse_history_request_count
        + source_session.request_count
        + stock_flow_request_count
    )
    if request_count > scope.max_requests:
        raise LiveRecoveryError(
            "live_recovery_request_budget_exceeded",
            f"used={request_count}, maximum={scope.max_requests}",
            status="FAILED_RETRYABLE",
        )

    files = {
        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in sorted(source_bundle.glob("*.json"))
        if path.is_file()
    }
    source_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "targetDate": target,
                "contextDates": context_dates,
                "files": files,
                "manifestSha256": _sha256(manifest_path),
                "qualitySha256": _sha256(quality_path),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    evidence = {
        "schemaVersion": 1,
        "status": "READY",
        "campaignId": scope.campaign_id,
        "recoveryId": scope.recovery_id,
        "planHash": scope.plan_hash,
        "runKey": scope.run_key,
        "windowDate": parsed_run_key["date"],
        "attemptCount": attempt,
        "dataDates": list(scope.data_dates),
        "targetDate": target,
        "contextDates": context_dates,
        "requiredStages": list(scope.required_stages),
        "producerBuildSha": producer_build_sha,
        "collectorRunId": run_id,
        "szseHistoryRequestCount": szse_history_request_count,
        "szseHistoryMaxRequests": szse_history_budget,
        "szseSourceMode": "current" if current_szse_matches else "history",
        "sourceRequestCount": source_session.request_count,
        "stockFlowRequestCount": stock_flow_request_count,
        "requestCount": request_count,
        "maxRequests": scope.max_requests,
        "postChildRequestReserve": post_child_request_reserve,
        "historyPublications": [
            {key: value for key, value in item.items() if key not in {"token", "secret", "authorization"}}
            for item in history_publications
            if isinstance(item, dict)
        ],
        "historyArchive": (
            {
                key: value
                for key, value in final_response.items()
                if key not in {"token", "secret", "authorization"}
            }
            if isinstance(final_response, dict)
            else None
        ),
        "stockFlowInputReport": (
            "stock-flow-fetch-report.json"
            if stock_flow_report_path is not None and stock_flow_report_path.is_file()
            else None
        ),
        "manifestSha256": _sha256(manifest_path),
        "qualitySha256": _sha256(quality_path),
        "sourceFingerprint": source_fingerprint,
        "files": files,
        "measuredAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    _write_json(source_bundle / "recovery-evidence.json", evidence)
    _write_json(artifact_root / "recovery-evidence.json", evidence)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-hash")
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--producer-build-sha", required=True)
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"))
    parser.add_argument("--failure-output", type=Path)
    args = parser.parse_args()
    plan = _read_json(args.plan)
    try:
        evidence = run_live_recovery(
            plan=plan,
            expected_plan_hash=args.expected_plan_hash,
            attempt=args.attempt,
            producer_build_sha=args.producer_build_sha,
            workspace_root=args.workspace_root.resolve(),
            base_url=args.base_url,
        )
    except (LiveRecoveryError, RecoveryContractError, ValueError) as exc:
        code = exc.code if isinstance(exc, (LiveRecoveryError, RecoveryContractError)) else "live_recovery_failed"
        status = exc.status if isinstance(exc, LiveRecoveryError) else "BLOCKED_QUALITY"
        # The plan is validated before its root can be trusted.  On a plan
        # validation failure use the explicitly supplied isolated failure path
        # (or a sibling of the plan) and never fall back to reports/memory.
        failure_path = args.failure_output
        if failure_path is None:
            failure_path = args.workspace_root / ".recovery" / "live-recovery-failure.json"
        assert_recovery_output_path(str(failure_path))
        _write_json(
            failure_path,
            {
                "schemaVersion": 1,
                "status": status,
                "errorCode": code,
                "detail": str(exc),
                "measuredAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
        )
        print(json.dumps({"status": status, "errorCode": code, "failure": str(failure_path)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps({"status": evidence["status"], "targetDate": evidence["targetDate"], "requestCount": evidence["requestCount"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
