from __future__ import annotations

import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

import pandas as pd

from .adapters import OfficialMarketAdapters
from .http import build_session
from .models import FetchArtifact, QualityFinding
from .pipeline_application import (
    ApplicationContractError,
    build_application_identity,
    build_checkpoint,
    canonical_sha256,
    execute_application_plan,
)
from .quality import (
    profile,
    summarize_findings,
    validate_connect_summary,
    validate_connect_top,
    validate_etf,
    validate_etf_nav,
    validate_etf_pcf,
    validate_bse_margin,
    validate_margin,
    validate_margin_security,
)
from .storage import new_run_id, now_iso, write_json, write_normalized_csv


# The PCF endpoint is one request for the basic record plus one request for
# the component list per ETF.  It is an optional enrichment for the daily
# release, not a reason to let the entire official-source collection run past
# its bounded window.  The full inventory/backfill path can request a larger
# cap explicitly; the daily default intentionally remains small and records
# every deferred code in the PCF artifact warning.
DEFAULT_PCF_MAX_CODES = 16


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
    those rows are accumulated from prior runs. The current target is mandatory
    for a complete release, but a missing target is represented as pending so
    the next window can reconcile it.
    """
    if dataset == "szse_etf":
        return [requested_dates[-1]]
    return requested_dates


_REQUIRED_COVERAGE_DATASETS = {
    "sse_etf",
    "szse_etf",
    "sse_margin",
    "szse_margin",
    "bse_margin",
    "stock_connect_summary",
}

# SZSE publishes its ETF scale and margin summaries after the SSE evening
# window.  These feeds are intentionally deferred by the evening collector;
# the morning/reconcile windows remain the single place where a complete
# cross-market snapshot can be produced.  Keeping this contract in one helper
# prevents the scheduler label from becoming a no-op hint again.
_LATE_RELEASE_DATASETS = ("szse_etf", "szse_margin")
_KNOWN_DATASETS = (
    "sse_etf",
    "sse_etf_nav",
    "sse_etf_pcf",
    "szse_etf",
    "sse_margin",
    "sse_margin_security",
    "szse_margin",
    "bse_margin",
    "stock_connect_summary",
    "stock_connect_top",
)


def _deferred_datasets_for_window(collection_window: str) -> list[str]:
    """Return source datasets intentionally deferred by one collection window.

    ``auto`` preserves the historical/manual behaviour and collects every
    source.  Only the explicitly scheduled ``evening`` window is source
    scoped; ``morning`` and ``reconcile`` are full-source windows and are the
    only windows eligible to replace the active release.
    """
    if collection_window not in {"auto", "evening", "morning", "reconcile"}:
        raise ValueError(f"unsupported collection window: {collection_window}")
    return list(_LATE_RELEASE_DATASETS) if collection_window == "evening" else []


def _pending_required_coverage_datasets(
    findings: list[QualityFinding],
) -> list[str]:
    """Return official domains that have not published every requested day.

    A date gap is a normal release-window state.  It is only considered
    pending when the finding is about required date coverage; schema, parser,
    authentication and transport errors remain hard failures.
    """
    pending: set[str] = set()
    for finding in findings:
        if (
            finding.check_id == "timeliness.requested_date_coverage"
            and finding.dataset in _REQUIRED_COVERAGE_DATASETS
            and finding.status == "warn"
            and finding.evidence.get("missing_dates")
        ):
            pending.add(finding.dataset)
    return sorted(pending)


def _resolve_phase0_gate(
    quality: dict[str, Any],
    errors: list[dict[str, Any]],
    pending_domains: list[str],
) -> str:
    """Resolve collection state without turning source timing into failure."""
    if quality["counts"]["fail"] or errors:
        return "blocked"
    if pending_domains:
        return "pending"
    if any(
        finding["status"] == "warn" and finding["severity"] in {"critical", "high"}
        for finding in quality["findings"]
    ):
        return "conditional_pass"
    return "pass"


def collect_phase0(
    *,
    dates: list[str],
    data_root: Path,
    report_root: Path,
    workspace_root: Path,
    collection_window: str = "auto",
    pcf_max_codes: int = DEFAULT_PCF_MAX_CODES,
    session: requests.Session | None = None,
    pcf_max_workers: int = 4,
    szse_etf_history_root: Path | None = None,
    application_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if len(dates) != 3:
        raise ValueError("Phase 0 requires exactly three requested trading dates")
    if collection_window not in {"auto", "evening", "morning", "reconcile"}:
        raise ValueError(f"unsupported collection window: {collection_window}")
    if not isinstance(pcf_max_codes, int) or pcf_max_codes < 0:
        raise ValueError("pcf_max_codes must be a non-negative integer")
    if not isinstance(pcf_max_workers, int) or pcf_max_workers <= 0:
        raise ValueError("pcf_max_workers must be a positive integer")
    if szse_etf_history_root is not None:
        if not isinstance(szse_etf_history_root, Path):
            raise ValueError("szse_etf_history_root must be a Path")
        if not szse_etf_history_root.is_dir():
            raise ValueError("szse_etf_history_root must be an existing directory")
    application_identity = None
    if application_context is not None:
        try:
            # Validate the shared identity before any adapter or filesystem
            # work begins.  A malformed resume/recovery context must fail
            # closed without leaving a partial collection behind.
            application_identity = build_application_identity(
                run_key=application_context["runKey"],
                window_date=application_context["windowDate"],
                target_date=application_context.get("targetDate"),
                producer_build_sha=application_context.get("producerBuildSha"),
            )
        except (KeyError, ApplicationContractError) as exc:
            raise ValueError(f"invalid application context: {exc}") from exc
    dates = sorted(dates)
    deferred_datasets = _deferred_datasets_for_window(collection_window)
    run_id = new_run_id()
    started_at = now_iso()
    collection_session = session or build_session()
    adapters = OfficialMarketAdapters(session=collection_session, data_root=data_root, run_id=run_id)
    frames: dict[str, list[pd.DataFrame]] = defaultdict(list)
    artifacts: list[FetchArtifact] = []
    errors: list[dict[str, Any]] = []
    optional_errors: list[dict[str, Any]] = []

    def run_step(
        name: str, operation: Callable[[], Any], *, required: bool = True
    ) -> Any | None:
        try:
            return operation()
        except Exception as exc:  # keep independent data domains observable
            # A bounded recovery transport uses this typed error to signal a
            # hard request-budget stop.  Do not turn that control decision
            # into a generic quality finding and continue issuing requests
            # for every remaining dataset.
            if str(getattr(exc, "code", "")).startswith("pipeline_lease_") or getattr(exc, "code", None) in {
                "live_recovery_request_budget_exceeded",
                "live_recovery_source_unavailable",
                "official_source_request_budget_exceeded",
                "official_source_unavailable",
            }:
                raise
            error = {
                "step": name,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
            (errors if required else optional_errors).append(error)
            return None

    for trade_date in dates:
        result = run_step(
            f"sse_etf:{trade_date}", lambda date=trade_date: adapters.fetch_sse_etf(date)
        )
        if result:
            frame, artifact = result
            frames["sse_etf"].append(frame)
            artifacts.append(artifact)

    # PCF is an optional date-aligned enrichment.  Fetch only ETFs whose official
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
            lambda: adapters.fetch_sse_etf_pcf(
                changed_codes,
                dates[-1],
                max_codes=pcf_max_codes,
                max_workers=pcf_max_workers,
            ),
        )
        if result:
            frame, artifact = result
            # Keep the optional PCF artifact even when the source is stale or
            # empty: its compressed raw response is useful evidence, but the
            # empty normalized side must remain an unavailable enrichment and
            # must never block the core dashboard publication.
            frames["sse_etf_pcf"].append(frame)
            artifacts.append(artifact)

    # The SSE ETF list publishes the previous trading day's unit NAV and may
    # advance its TRADING_DAY before the next collection window.  Keep a
    # same-window artifact for every ETF snapshot date; the adapter accepts
    # only an exact publication or the single next publication for the latest
    # requested share date.  Older requested dates remain empty instead of
    # silently receiving the moving current NAV.  NAV is optional enrichment
    # and must never block the share snapshot from being published.
    for publication_date in dates:
        result = run_step(
            f"sse_etf_nav:{publication_date}",
            lambda date=publication_date: adapters.fetch_sse_etf_nav(date, dates),
        )
        if result:
            frame, artifact = result
            frames["sse_etf_nav"].append(frame)
            artifacts.append(artifact)

    if "szse_etf" not in deferred_datasets:
        if szse_etf_history_root is None:
            result = run_step(
                "szse_etf:current", lambda: adapters.fetch_szse_etf_current(dates)
            )
            if result:
                frame, artifact = result
                frames["szse_etf"].append(frame)
                artifacts.append(artifact)
        else:
            for trade_date in dates:
                result = run_step(
                    f"szse_etf:history:{trade_date}",
                    lambda date=trade_date: adapters.fetch_szse_etf_history(
                        szse_etf_history_root, date
                    ),
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

    # Security-level margin detail is an optional stock-pressure enrichment.
    # It is collected independently for each requested date so a missing
    # detail response cannot block the market-level snapshot or be replaced by
    # a newer date.
    for trade_date in dates:
        result = run_step(
            f"sse_margin_security:{trade_date}",
            lambda date=trade_date: adapters.fetch_sse_margin_security(date),
            required=False,
        )
        if result:
            frame, artifact = result
            frames["sse_margin_security"].append(frame)
            artifacts.append(artifact)

    if "szse_margin" not in deferred_datasets:
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
            f"bse_margin:{trade_date}",
            lambda date=trade_date: adapters.fetch_bse_margin(date),
        )
        if result:
            frame, artifact = result
            frames["bse_margin"].append(frame)
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

    # Source-scoped windows deliberately leave deferred datasets untouched.
    # Still materialize an empty, schema-valid CSV for every known dataset so
    # downstream snapshot builders can distinguish "not collected in this
    # window" from a missing path or a malformed manifest.
    for dataset in _KNOWN_DATASETS:
        if not frames.get(dataset):
            frames[dataset].append(pd.DataFrame())

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
    # Historical rows without an explicitly aligned NAV match remain null.
    # This preserves the scale source's raw-to-normalized reconciliation while
    # making the derived dashboard calculation date-safe.
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
        "sse_margin_security": validate_margin_security,
        "szse_margin": validate_margin,
        "bse_margin": validate_bse_margin,
        "stock_connect_summary": lambda frame, _: validate_connect_summary(frame),
        "stock_connect_top": lambda frame, _: validate_connect_top(frame),
    }
    findings: list[QualityFinding] = []
    profiles: dict[str, dict[str, Any]] = {}
    for dataset in validators:
        frame = combined.get(dataset, pd.DataFrame())
        profiles[dataset] = profile(frame)
        findings.extend(validators[dataset](frame, dataset))

    for error in optional_errors:
        findings.append(
            QualityFinding(
                check_id="collection.optional_step",
                dataset=str(error["step"]).split(":", 1)[0],
                status="warn",
                severity="high",
                message="可选增强采集失败；核心数据未被阻断",
                evidence={
                    "step": error["step"],
                    "error_type": error["error_type"],
                    "message": error["message"],
                },
                remediation="保留上一完整发布；在下一 reconcile 窗口重试可选增强源。",
            )
        )

    for dataset in ("sse_etf", "sse_margin", "szse_margin", "bse_margin", "stock_connect_summary"):
        observed_dates = profiles.get(dataset, {}).get("distinct_trade_dates", [])
        findings.append(
            _coverage_finding(
                dataset,
                observed_dates,
                dates,
                warn_only=True,
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
            # day must be present before a run can be considered ready. A
            # stale HTTP 200 response becomes an explicit pending state.
            _required_coverage_dates("szse_etf", dates),
            warn_only=True,
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
            remediation="上交所单位净值只接受精确公告或最新份额日的次日公告对齐；缺失时保留份额数据，不把新公告日净值倒灌到更早历史日。",
        )
    )
    findings.append(
        _coverage_finding(
            "sse_margin_security",
            profiles.get("sse_margin_security", {}).get("distinct_trade_dates", []),
            dates,
            warn_only=True,
            remediation="上交所个股两融明细属于可选增强；缺失时个股页面保留未知，不用 0 或上一日值补齐。",
        )
    )

    quality = summarize_findings(findings)
    pending_domains = _pending_required_coverage_datasets(findings)
    gate_status = _resolve_phase0_gate(quality, errors, pending_domains)
    quality["phase0_gate_status"] = gate_status
    quality["phase0_gate_passed"] = gate_status == "pass"
    quality["release_eligible"] = gate_status in {"pass", "conditional_pass"}
    quality["pending_domains"] = pending_domains
    quality["phase1_entry_recommendation"] = (
        "wait_for_sources"
        if gate_status == "pending"
        else (
            "proceed_with_szse_forward_snapshot_constraint"
            if gate_status == "conditional_pass"
            else ("proceed" if gate_status == "pass" else "do_not_proceed")
        )
    )
    quality["collection_window"] = collection_window
    quality["collection_scope"] = "early" if deferred_datasets else "full"
    quality["deferred_datasets"] = deferred_datasets
    quality["profiles"] = profiles
    quality["errors"] = errors
    quality["optional_errors"] = optional_errors
    szse_frame = combined.get("szse_etf", pd.DataFrame())
    szse_page_dates = (
        []
        if szse_frame.empty or "source_page_date" not in szse_frame.columns
        else [
            str(value)
            for value in szse_frame["source_page_date"].dropna().astype(str).tolist()
            if str(value).strip() and str(value).lower() != "nan"
        ]
    )
    szse_trade_dates = (
        []
        if szse_frame.empty or "trade_date" not in szse_frame.columns
        else [str(value) for value in szse_frame["trade_date"].dropna().astype(str).tolist()]
    )
    manifest = {
        "run_id": run_id,
        "collection_window": collection_window,
        "collection_scope": "early" if deferred_datasets else "full",
        "deferred_datasets": deferred_datasets,
        "pcf_max_codes": pcf_max_codes,
        "started_at": started_at,
        "finished_at": now_iso(),
        "requested_dates": dates,
        "szse_etf_page_date": max(szse_page_dates) if szse_page_dates else None,
        "szse_etf_trade_date": max(szse_trade_dates) if szse_trade_dates else None,
        "artifacts": [],
        "optional_errors": optional_errors,
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

    # v0.6-A is an additive extraction seam.  Existing daily callers keep the
    # legacy manifest unchanged; the recovery caller opts in with its already
    # validated runKey/target identity so the same collector output carries a
    # deterministic application checkpoint without giving the checkpoint any
    # new publishing authority.
    if application_identity is not None:
        try:
            identity = application_identity
            input_sha = canonical_sha256(
                {
                    "requestedDates": dates,
                    "collectionWindow": collection_window,
                    "pcfMaxCodes": pcf_max_codes,
                }
            )
            output_sha = canonical_sha256({"manifest": manifest, "quality": quality})
            checkpoint_status = "COMPLETED" if gate_status in {"pass", "conditional_pass"} else (
                "PENDING" if gate_status == "pending" else "BLOCKED_QUALITY"
            )
            execution = execute_application_plan(
                identity=identity,
                stages=["core_collection"],
                stage_inputs={"core_collection": input_sha},
                stage_runner=lambda stage, digest: build_checkpoint(
                    identity=identity,
                    stage=stage,
                    status=checkpoint_status,
                    input_sha256=digest,
                    output_sha256=output_sha if checkpoint_status == "COMPLETED" else None,
                    allowed_side_effects=("filesystem:data", "filesystem:reports"),
                    evidence={
                        "phase0GateStatus": gate_status,
                        "requestedDates": dates,
                        "artifactCount": len(artifacts),
                    },
                ),
            )
            manifest["application"] = {
                "schemaVersion": execution["schemaVersion"],
                "contractVersion": execution["contractVersion"],
                "identity": identity.as_dict(),
                "planHash": execution["planHash"],
                "checkpoints": execution["checkpoints"],
            }
        except ApplicationContractError as exc:
            raise ValueError(f"invalid application context: {exc}") from exc

    run_report_dir = report_root / run_id
    write_json(run_report_dir / "manifest.json", manifest)
    write_json(run_report_dir / "quality-summary.json", quality)
    write_json(report_root / "latest-manifest.json", manifest)
    write_json(report_root / "latest-quality-summary.json", quality)
    return {"manifest": manifest, "quality": quality}
