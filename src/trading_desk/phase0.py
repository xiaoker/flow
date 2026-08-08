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

    validators: dict[str, Callable[[pd.DataFrame, str], list[QualityFinding]]] = {
        "sse_etf": validate_etf,
        "szse_etf": validate_etf,
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
            dates,
            warn_only=True,
            remediation="深交所只提供当前快照；从正式上线日起每日留存，不宣称可官方回填。",
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
