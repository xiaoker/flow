from __future__ import annotations

import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .adapters import OfficialMarketAdapters
from .http import build_session
from .models import FetchArtifact, QualityFinding
from .quality import (
    profile,
    summarize_findings,
    validate_connect_summary,
    validate_connect_top,
    validate_etf,
    validate_etf_nav,
    validate_etf_pcf,
    validate_margin,
)
from .storage import new_run_id, now_iso, write_json, write_normalized_csv


def _relative(path: str | Path, workspace_root: Path) -> str:
    candidate = Path(str(path).split(";")[0])
    try:
        first = str(candidate.resolve().relative_to(workspace_root.resolve()))
    except ValueError:
        first = str(candidate)
    if ";" not in str(path):
        return first
    second_candidate = Path(str(path).split(";", 1)[1])
    try:
        second = str(second_candidate.resolve().relative_to(workspace_root.resolve()))
    except ValueError:
        second = str(second_candidate)
    return f"{first};{second}"


def _coverage_finding(
    dataset: str,
    observed_dates: list[str],
    expected_dates: list[str],
    *,
    warn_only: bool = False,
    remediation: str,
) -> QualityFinding:
    missing = sorted(set(expected_dates) - set(observed_dates))
    return QualityFinding(
        check_id="timeliness.requested_date_coverage",
        dataset=dataset,
        status="pass" if not missing else ("warn" if warn_only else "fail"),
        severity="info" if not missing else "high",
        message="覆盖全部请求日期" if not missing else "未覆盖全部请求日期",
        evidence={
            "expected_dates": expected_dates,
            "observed_dates": observed_dates,
            "missing_dates": missing,
        },
        remediation=None if not missing else remediation,
    )


def _required_coverage_dates(dataset: str, requested_dates: list[str]) -> list[str]:
    """Return the dates that must be present before a dataset is publishable.

    The SZSE ETF endpoint is a current snapshot, not a historical endpoint. It
    cannot be required to return every date in the three-day collection window;
    those rows are accumulated from prior runs. The current target, however,
    is mandatory so an old HTTP 200 response cannot be mistaken for readiness.
    """
    if dataset == "szse_etf":
        return [requested_dates[-1]]
    return requested_dates


def collect_phase0(
    *,
    dates: list[str],
    data_root: Path,
    report_root: Path,
    workspace_root: Path,
    collection_window: str = "auto",
) -> dict[str, Any]:
    if len(dates) != 3:
        raise ValueError("Phase 0 requires exactly three requested trading dates")
    if collection_window not in {"auto", "evening", "morning", "reconcile"}:
        raise ValueError(f"unsupported collection window: {collection_window}")
    dates = sorted(dates)
    run_id = new_run_id()
    started_at = now_iso()
    session = build_session()
    adapters = OfficialMarketAdapters(session=session, data_root=data_root, run_id=run_id)
    frames: dict[str, list[pd.DataFrame]] = defaultdict(list)
    artifacts: list[FetchArtifact] = []
    errors: list[dict[str, Any]] = []

    def run_step(name: str, operation: Callable[[], Any]) -> Any | None:
        try:
            return operation()
        except Exception as exc:  # keep independent data domains observable
            errors.append(
                {
                    "step": name,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
            )
            return None

    for trade_date in dates:
        result = run_step(
            f"sse_etf:{trade_date}", lambda date=trade_date: adapters.fetch_sse_etf(date)
        )
        if result:
            frame, artifact = result
            frames["sse_etf"].append(frame)
            artifacts.append(artifact)

    # PCF is an optional, same-day enrichment.  Fetch only ETFs whose official
    # total shares changed between the two latest requested dates.  This keeps
    # the request count bounded while preserving exact coverage for every
    # non-zero ETF flow.  The compressed response artifact is kept even when
    # the endpoint is stale or empty, but a stale 200 response can never block
    # publication or be used as a prior-day fallback.
    if collection_window in {"auto", "morning", "reconcile"}:
        sse_share_parts = [part for part in frames.get("sse_etf", []) if not part.empty]
        changed_codes: list[str] = []
        if sse_share_parts:
            sse_share_window = pd.concat(sse_share_parts, ignore_index=True)
            sse_share_window["trade_date"] = sse_share_window["trade_date"].astype(str)
            sse_share_window["instrument_code"] = sse_share_window["instrument_code"].astype(str).str.zfill(6)
            latest = sse_share_window.loc[
                sse_share_window["trade_date"].eq(dates[-1]),
                ["instrument_code", "shares"],
            ].rename(columns={"shares": "latest_shares"})
            previous = sse_share_window.loc[
                sse_share_window["trade_date"].eq(dates[-2]),
                ["instrument_code", "shares"],
            ].rename(columns={"shares": "previous_shares"})
            changed = latest.merge(previous, on="instrument_code", how="inner")
            changed["share_delta"] = pd.to_numeric(
                changed["latest_shares"], errors="coerce"
            ).sub(pd.to_numeric(changed["previous_shares"], errors="coerce"))
            changed_codes = (
                changed.loc[changed["share_delta"].ne(0)]
                .sort_values("share_delta", key=lambda series: series.abs(), ascending=False)["instrument_code"]
                .astype(str)
                .tolist()
            )
        result = run_step(
            f"sse_etf_pcf:{dates[-1]}",
            lambda: adapters.fetch_sse_etf_pcf(changed_codes, dates[-1]),
        )
        if result:
            frame, artifact = result
            # Keep the optional PCF artifact even when the source is stale or
            # empty: its compressed raw response is useful evidence, but the
            # empty normalized side must remain an unavailable enrichment and
            # must never block the core dashboard publication.
            frames["sse_etf_pcf"].append(frame)
            artifacts.append(artifact)

    # The SSE ETF list publishes the previous trading day's unit NAV.  Keep a
    # same-window artifact for every ETF snapshot date so the raw lineage is
    # date-aligned.  The public list may only return the latest publication;
    # the adapter filters exact dates and leaves older requested dates empty
    # instead of silently reusing a newer NAV.  NAV is optional enrichment and
    # must never block the share snapshot from being published.
    for publication_date in dates:
        result = run_step(
            f"sse_etf_nav:{publication_date}",
            lambda date=publication_date: adapters.fetch_sse_etf_nav(date, dates),
        )
        if result:
            frame, artifact = result
            frames["sse_etf_nav"].append(frame)
            artifacts.append(artifact)

    result = run_step(
        "szse_etf:current", lambda: adapters.fetch_szse_etf_current(dates)
    )
    if result:
        frame, artifact = result
        frames["szse_etf"].append(frame)
        artifacts.append(artifact)

    result = run_step(
        f"sse_margin:{dates[0]}:{dates[-1]}",
        lambda: adapters.fetch_sse_margin(dates[0], dates[-1]),
    )
    if result:
        frame, artifact = result
        frames["sse_margin"].append(frame)
        artifacts.append(artifact)

    for trade_date in dates:
        result = run_step(
            f"szse_margin:{trade_date}",
            lambda date=trade_date: adapters.fetch_szse_margin(date),
        )
        if result:
            frame, artifact = result
            frames["szse_margin"].append(frame)
            artifacts.append(artifact)

    for trade_date in dates:
        result = run_step(
            f"hkex_connect:{trade_date}",
            lambda date=trade_date: adapters.fetch_hkex_connect(date),
        )
        if result:
            summary, top, artifact = result
            frames["stock_connect_summary"].append(summary)
            frames["stock_connect_top"].append(top)
            artifacts.append(artifact)

    combined: dict[str, pd.DataFrame] = {}
    combined_paths: dict[str, str] = {}
    for dataset, parts in frames.items():
        nonempty_parts = [part for part in parts if not part.empty]
        if nonempty_parts:
            combined_frame = pd.concat(nonempty_parts, ignore_index=True)
        elif parts:
            combined_frame = parts[0].copy()
        else:
            combined_frame = pd.DataFrame()
        combined[dataset] = combined_frame
        path = write_normalized_csv(
            root=data_root,
            dataset=f"phase0_combined_{dataset}",
            partition=run_id,
            run_id=run_id,
            frame=combined_frame,
        )
        combined_paths[dataset] = _relative(path, workspace_root)

    # Enrich the share frame only after both official feeds have been written.
    # Historical rows without an exact NAV match remain null.  This preserves
    # the scale source's raw-to-normalized reconciliation while making the
    # derived dashboard calculation date-safe.
    sse_frame = combined.get("sse_etf", pd.DataFrame()).copy()
    nav_frame = combined.get("sse_etf_nav", pd.DataFrame())
    if not sse_frame.empty:
        if "nav" not in sse_frame.columns:
            sse_frame["nav"] = pd.NA
        if "nav_date" not in sse_frame.columns:
            sse_frame["nav_date"] = pd.NA
        if not nav_frame.empty and {"trade_date", "market", "instrument_code", "nav"}.issubset(nav_frame.columns):
            lookup = nav_frame.copy()
            lookup["trade_date"] = lookup["trade_date"].astype(str)
            lookup["instrument_code"] = lookup["instrument_code"].astype(str).str.zfill(6)
            lookup = lookup.drop_duplicates(
                ["trade_date", "market", "instrument_code"], keep="last"
            )
            sse_frame["trade_date"] = sse_frame["trade_date"].astype(str)
            sse_frame["instrument_code"] = sse_frame["instrument_code"].astype(str).str.zfill(6)
            sse_frame = sse_frame.drop(columns=["nav", "nav_date"], errors="ignore").merge(
                lookup[
                    ["trade_date", "market", "instrument_code", "nav", "nav_date"]
                ],
                on=["trade_date", "market", "instrument_code"],
                how="left",
            )
        combined["sse_etf"] = sse_frame
        enriched_path = write_normalized_csv(
            root=data_root,
            dataset="phase0_combined_sse_etf_enriched",
            partition=run_id,
            run_id=run_id,
            frame=sse_frame,
        )
        combined_paths["sse_etf"] = _relative(enriched_path, workspace_root)

    validators: dict[str, Callable[[pd.DataFrame, str], list[QualityFinding]]] = {
        "sse_etf": validate_etf,
        "szse_etf": validate_etf,
        "sse_etf_nav": validate_etf_nav,
        "sse_etf_pcf": validate_etf_pcf,
        "sse_margin": validate_margin,
        "szse_margin": validate_margin,
        "stock_connect_summary": lambda frame, _: validate_connect_summary(frame),
        "stock_connect_top": lambda frame, _: validate_connect_top(frame),
    }
    findings: list[QualityFinding] = []
    profiles: dict[str, dict[str, Any]] = {}
    for dataset in validators:
        frame = combined.get(dataset, pd.DataFrame())
        profiles[dataset] = profile(frame)
        findings.extend(validators[dataset](frame, dataset))

    for dataset in ("sse_etf", "sse_margin", "szse_margin", "stock_connect_summary"):
        observed_dates = profiles.get(dataset, {}).get("distinct_trade_dates", [])
        findings.append(
            _coverage_finding(
                dataset,
                observed_dates,
                dates,
                remediation="确认源站发布时间；若日期本应可用，则保持旧版本并补跑。",
            )
        )
    findings.append(
        _coverage_finding(
            "szse_etf",
            profiles.get("szse_etf", {}).get("distinct_trade_dates", []),
            # SZSE exposes a current snapshot rather than a historical time
            # series.  Historical coverage is therefore expected to be
            # accumulated in our archive, but the current requested trading
            # day must be present before a run can be considered ready.  The
            # previous warn-only check allowed a stale HTTP 200 response to
            # pass the gate and left the dashboard on an older day.
            _required_coverage_dates("szse_etf", dates),
            remediation=(
                "深交所只提供当前快照；确认页面日期已推进到目标交易日后再发布。"
                "旧日期响应必须保留原始证据，但不能替换 ACTIVE 版本。"
            ),
        )
    )
    findings.append(
        _coverage_finding(
            "sse_etf_nav",
            profiles.get("sse_etf_nav", {}).get("distinct_trade_dates", []),
            dates,
            warn_only=True,
            remediation="上交所单位净值只按公告日精确匹配；缺失时保留份额数据并等待该日官方发布，不把新公告日净值倒灌到历史日。",
        )
    )

    quality = summarize_findings(findings)
    high_warnings = [
        finding
        for finding in quality["findings"]
        if finding["status"] == "warn" and finding["severity"] in {"critical", "high"}
    ]
    if quality["counts"]["fail"] or errors:
        gate_status = "blocked"
    elif high_warnings:
        gate_status = "conditional_pass"
    else:
        gate_status = "pass"
    quality["phase0_gate_status"] = gate_status
    quality["phase0_gate_passed"] = gate_status == "pass"
    quality["phase1_entry_recommendation"] = (
        "proceed_with_szse_forward_snapshot_constraint"
        if gate_status == "conditional_pass"
        else ("proceed" if gate_status == "pass" else "do_not_proceed")
    )
    quality["profiles"] = profiles
    quality["errors"] = errors
    manifest = {
        "run_id": run_id,
        "collection_window": collection_window,
        "started_at": started_at,
        "finished_at": now_iso(),
        "requested_dates": dates,
        "szse_etf_page_date": (
            None
            if combined.get("szse_etf", pd.DataFrame()).empty
            else str(combined["szse_etf"]["source_page_date"].max())
        ),
        "szse_etf_trade_date": (
            None
            if combined.get("szse_etf", pd.DataFrame()).empty
            else str(combined["szse_etf"]["trade_date"].max())
        ),
        "artifacts": [],
        "combined_paths": combined_paths,
        "quality_summary_path": f"reports/phase0/{run_id}/quality-summary.json",
    }
    for artifact in artifacts:
        payload = artifact.to_dict()
        payload["raw_path"] = _relative(artifact.raw_path, workspace_root)
        payload["metadata_path"] = _relative(artifact.metadata_path, workspace_root)
        if artifact.normalized_path:
            payload["normalized_path"] = _relative(
                artifact.normalized_path, workspace_root
            )
        manifest["artifacts"].append(payload)

    run_report_dir = report_root / run_id
    write_json(run_report_dir / "manifest.json", manifest)
    write_json(run_report_dir / "quality-summary.json", quality)
    write_json(report_root / "latest-manifest.json", manifest)
    write_json(report_root / "latest-quality-summary.json", quality)
    return {"manifest": manifest, "quality": quality}
