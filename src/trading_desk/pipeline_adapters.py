"""Bounded adapters for real, side-effect-classified pipeline stages.

The first adapter reuses the existing official-data collector.  It is not
registered by the fixture CLI or production workflows: a trusted caller must
explicitly inject it, verify the lease/authorization, and provide a canonical
stage configuration.  The adapter writes only into its isolated workspace;
it never publishes to D1/R2 or changes an active release.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

import requests

from .http import HttpSettings, build_session
from .phase0 import DEFAULT_PCF_MAX_CODES, collect_phase0
from .pipeline_application import ApplicationIdentity, canonical_sha256


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COLLECTION_WINDOWS = {"evening", "morning", "reconcile"}
_MAX_SOURCE_REQUESTS = 100
_MIN_SOURCE_INTERVAL_SECONDS = 2.0
_MAX_SOURCE_INTERVAL_SECONDS = 30.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie"}


class PipelineAdapterError(RuntimeError):
    """A machine-readable, fail-closed stage-adapter error."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


class BoundedOfficialSession:
    """Count and pace every HTTP request made by the official-data adapter.

    Automatic retries are disabled in the underlying requests session so the
    request ceiling counts actual attempts.  403/429 responses stop the
    collector immediately instead of being converted into a soft warning.
    """

    def __init__(
        self,
        *,
        maximum: int,
        min_interval_seconds: float,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= _MAX_SOURCE_REQUESTS
        ):
            raise PipelineAdapterError("invalid_source_request_budget")
        if (
            isinstance(min_interval_seconds, bool)
            or not isinstance(min_interval_seconds, (int, float))
            or not math.isfinite(float(min_interval_seconds))
            or not _MIN_SOURCE_INTERVAL_SECONDS
            <= float(min_interval_seconds)
            <= _MAX_SOURCE_INTERVAL_SECONDS
        ):
            raise PipelineAdapterError("invalid_source_request_interval")
        self._session = session if session is not None else build_session(HttpSettings(total_retries=0))
        adapters = getattr(self._session, "adapters", None)
        if isinstance(adapters, Mapping) and any(
            getattr(getattr(adapter, "max_retries", None), "total", None) not in {0, False}
            for adapter in adapters.values()
        ):
            raise PipelineAdapterError("unaccounted_source_retries_forbidden")
        self.maximum = maximum
        self.min_interval_seconds = float(min_interval_seconds)
        self.request_count = 0
        self._last_by_host: dict[str, float] = {}
        self._monotonic = monotonic
        self._sleep = sleep

    @property
    def headers(self) -> Any:
        return self._session.headers

    @property
    def request_timeout(self) -> tuple[float, float]:
        return getattr(self._session, "request_timeout", (8.0, 25.0))

    @request_timeout.setter
    def request_timeout(self, value: tuple[float, float]) -> None:
        setattr(self._session, "request_timeout", value)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        current_url = url
        request_kwargs = dict(kwargs)
        request_kwargs.pop("allow_redirects", None)
        previous_origin = urlparse(current_url).netloc.lower()

        for redirect_count in range(_MAX_REDIRECTS + 1):
            self._before_request(current_url)
            # Disable requests' hidden redirect loop so each real HTTP attempt
            # is budgeted and paced here, including every redirect hop.
            response = self._session.get(
                current_url,
                allow_redirects=False,
                **request_kwargs,
            )
            status = getattr(response, "status_code", None)
            if status in {403, 429}:
                self._close_response(response)
                raise PipelineAdapterError(
                    "official_source_unavailable",
                    f"HTTP {status}",
                )
            if status not in _REDIRECT_STATUSES:
                return response
            if redirect_count >= _MAX_REDIRECTS:
                self._close_response(response)
                raise PipelineAdapterError("official_source_redirect_limit_exceeded")

            headers = getattr(response, "headers", {})
            location = headers.get("Location") if isinstance(headers, Mapping) else None
            if not isinstance(location, str) or not location.strip():
                self._close_response(response)
                raise PipelineAdapterError("official_source_redirect_location_missing")
            next_url = urljoin(current_url, location.strip())
            next_parsed = urlparse(next_url)
            if (
                next_parsed.scheme not in {"http", "https"}
                or not next_parsed.hostname
                or next_parsed.username is not None
                or next_parsed.password is not None
            ):
                self._close_response(response)
                raise PipelineAdapterError("official_source_redirect_url_invalid")

            next_origin = next_parsed.netloc.lower()
            if next_origin != previous_origin:
                session_auth = getattr(self._session, "auth", None)
                session_headers = getattr(self._session, "headers", {})
                has_session_secret_header = any(
                    str(key).lower() in _SENSITIVE_HEADERS
                    for key in dict(session_headers).keys()
                )
                session_cookies = getattr(self._session, "cookies", None)
                has_session_cookies = False
                if session_cookies is not None:
                    try:
                        has_session_cookies = len(session_cookies) > 0
                    except TypeError:
                        has_session_cookies = True
                if session_auth is not None or has_session_secret_header or has_session_cookies:
                    self._close_response(response)
                    raise PipelineAdapterError("cross_origin_redirect_with_session_auth_forbidden")
                safe_headers = {
                    key: value
                    for key, value in dict(session_headers).items()
                    if str(key).lower() not in _SENSITIVE_HEADERS
                }
                request_headers = request_kwargs.get("headers", {})
                if isinstance(request_headers, Mapping):
                    safe_headers.update(
                        {
                            key: value
                            for key, value in request_headers.items()
                            if str(key).lower() not in _SENSITIVE_HEADERS
                        }
                    )
                request_kwargs["headers"] = safe_headers
                request_kwargs.pop("auth", None)
                request_kwargs.pop("cookies", None)

            self._close_response(response)
            current_url = next_url
            previous_origin = next_origin
            # Query parameters belong to the initial URL and must not be
            # appended again to a redirect target.
            request_kwargs.pop("params", None)

        raise PipelineAdapterError("official_source_redirect_limit_exceeded")

    @staticmethod
    def _close_response(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    def _before_request(self, url: str) -> None:
        if self.request_count >= self.maximum:
            raise PipelineAdapterError("official_source_request_budget_exceeded")
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PipelineAdapterError("official_source_request_url_invalid")
        host = parsed.hostname.lower()
        now = self._monotonic()
        previous = self._last_by_host.get(host)
        if previous is not None:
            delay = self.min_interval_seconds - (now - previous)
            if delay > 0:
                self._sleep(delay)
        self.request_count += 1
        self._last_by_host[host] = self._monotonic()


def _validated_config(value: Mapping[str, Any], identity: ApplicationIdentity) -> dict[str, Any]:
    allowed = {
        "dates",
        "collectionWindow",
        "pcfMaxCodes",
        "maxRequests",
        "minIntervalSeconds",
        "sourceFingerprint",
    }
    if set(value) != allowed:
        raise PipelineAdapterError("invalid_core_collection_config_fields")

    dates = value.get("dates")
    if (
        not isinstance(dates, list)
        or len(dates) != 3
        or any(not isinstance(item, str) for item in dates)
        or len(set(dates)) != 3
    ):
        raise PipelineAdapterError("core_collection_requires_three_dates")
    try:
        parsed_dates = [date.fromisoformat(item) for item in dates]
    except ValueError as exc:
        raise PipelineAdapterError("invalid_core_collection_date") from exc
    if any(parsed.isoformat() != raw for parsed, raw in zip(parsed_dates, dates)):
        raise PipelineAdapterError("invalid_core_collection_date")
    if parsed_dates != sorted(parsed_dates):
        raise PipelineAdapterError("core_collection_dates_must_be_ascending")
    if identity.target_date is not None and identity.target_date not in dates:
        raise PipelineAdapterError("target_date_outside_collection_dates")

    window = value.get("collectionWindow")
    if window not in _COLLECTION_WINDOWS:
        raise PipelineAdapterError("invalid_core_collection_window")

    pcf_max_codes = value.get("pcfMaxCodes")
    if (
        isinstance(pcf_max_codes, bool)
        or not isinstance(pcf_max_codes, int)
        or not 0 <= pcf_max_codes <= DEFAULT_PCF_MAX_CODES
    ):
        raise PipelineAdapterError("invalid_core_collection_pcf_limit")

    max_requests = value.get("maxRequests")
    if (
        isinstance(max_requests, bool)
        or not isinstance(max_requests, int)
        or not 1 <= max_requests <= _MAX_SOURCE_REQUESTS
    ):
        raise PipelineAdapterError("invalid_source_request_budget")

    interval = value.get("minIntervalSeconds")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(float(interval))
        or not _MIN_SOURCE_INTERVAL_SECONDS
        <= float(interval)
        <= _MAX_SOURCE_INTERVAL_SECONDS
    ):
        raise PipelineAdapterError("invalid_source_request_interval")

    fingerprint = value.get("sourceFingerprint")
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        raise PipelineAdapterError("invalid_source_fingerprint")

    return {
        "dates": list(dates),
        "collectionWindow": window,
        "pcfMaxCodes": pcf_max_codes,
        "maxRequests": max_requests,
        "minIntervalSeconds": float(interval),
        "sourceFingerprint": fingerprint,
    }


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PipelineAdapterError("core_collection_output_missing", path.name) from exc


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineAdapterError("core_collection_receipt_invalid") from exc
    if not isinstance(value, Mapping):
        raise PipelineAdapterError("core_collection_receipt_invalid")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CoreCollectionStageAdapter:
    """Run the existing bounded-window collector into an isolated workspace."""

    allowed_side_effects: tuple[str, ...] = (
        "official_source_requests",
        "filesystem:isolated_stage_workspace",
    )
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False, compare=False)
    _monotonic: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def run(
        self,
        *,
        context: Any,
        input_sha256: str,
        config: Mapping[str, Any],
        workspace: Path,
    ) -> Mapping[str, Any]:
        if canonical_sha256(config) != input_sha256:
            raise PipelineAdapterError("core_collection_input_digest_mismatch")
        normalized = _validated_config(config, context.identity)

        workspace = workspace.resolve()
        receipt_path = workspace / "adapter-receipt.json"
        manifest_path = workspace / "phase0_reports" / "latest-manifest.json"
        quality_path = workspace / "phase0_reports" / "latest-quality-summary.json"

        if receipt_path.exists():
            return self._verified_receipt(
                identity=context.identity,
                input_sha256=input_sha256,
                workspace=workspace,
            )

        if workspace.exists() and any(workspace.iterdir()):
            # Do not re-request official data after a process dies mid-stage.
            # A later bounded reconciliation must inspect the partial evidence.
            raise PipelineAdapterError("partial_core_collection_requires_reconciliation")

        workspace.mkdir(parents=True, exist_ok=True)
        request_session = BoundedOfficialSession(
            maximum=normalized["maxRequests"],
            min_interval_seconds=normalized["minIntervalSeconds"],
            monotonic=self._monotonic,
            sleep=self._sleep,
        )
        try:
            result = collect_phase0(
                dates=normalized["dates"],
                data_root=workspace / "data",
                report_root=workspace / "phase0_reports",
                workspace_root=workspace,
                collection_window=normalized["collectionWindow"],
                pcf_max_codes=normalized["pcfMaxCodes"],
                session=request_session,  # type: ignore[arg-type]
                pcf_max_workers=1,
            )
        except PipelineAdapterError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in {"official_source_request_budget_exceeded", "official_source_unavailable"}:
                raise PipelineAdapterError(str(code)) from exc
            raise PipelineAdapterError("core_collection_adapter_failed", type(exc).__name__) from exc

        manifest = result.get("manifest") if isinstance(result, Mapping) else None
        quality = result.get("quality") if isinstance(result, Mapping) else None
        if not isinstance(manifest, Mapping) or not isinstance(quality, Mapping):
            raise PipelineAdapterError("core_collection_result_invalid")
        gate_status = quality.get("phase0_gate_status")
        status_by_gate = {
            "pass": "COMPLETED",
            "conditional_pass": "COMPLETED",
            "pending": "PENDING",
            "blocked": "BLOCKED_QUALITY",
        }
        status = status_by_gate.get(gate_status)
        if status is None:
            raise PipelineAdapterError("unknown_core_collection_gate_status")
        manifest_sha = _sha256_file(manifest_path)
        quality_sha = _sha256_file(quality_path)
        output_sha = canonical_sha256(
            {"manifestSha256": manifest_sha, "qualitySha256": quality_sha}
        )
        stage_result: dict[str, Any] = {
            "status": status,
            "outputSha256": output_sha,
            "allowedSideEffects": list(self.allowed_side_effects),
            "evidence": {
                "phase0GateStatus": gate_status,
                "requestedDates": normalized["dates"],
                "sourceFingerprint": normalized["sourceFingerprint"],
                "sourceRequestCount": request_session.request_count,
                "sourceRequestBudget": normalized["maxRequests"],
                "artifactCount": (
                    len(manifest.get("artifacts", []))
                    if isinstance(manifest.get("artifacts"), list)
                    else 0
                ),
                "manifestSha256": manifest_sha,
                "qualitySha256": quality_sha,
            },
        }
        _atomic_json(
            receipt_path,
            {
                "schemaVersion": 1,
                "identity": context.identity.as_dict(),
                "inputSha256": input_sha256,
                "configSha256": canonical_sha256(normalized),
                "sourceFingerprint": normalized["sourceFingerprint"],
                "manifestSha256": manifest_sha,
                "qualitySha256": quality_sha,
                "stageResult": stage_result,
            },
        )
        return stage_result

    def verify_checkpoint(
        self,
        *,
        checkpoint: Mapping[str, Any],
        identity: ApplicationIdentity,
        input_sha256: str,
        workspace: Path,
    ) -> bool:
        """Verify local evidence before the executor reuses a completion."""

        try:
            stage_result = self._verified_receipt(
                identity=identity,
                input_sha256=input_sha256,
                workspace=workspace.resolve(),
            )
        except PipelineAdapterError:
            return False
        return (
            checkpoint.get("identity") == identity.as_dict()
            and checkpoint.get("stage") == "core_collection"
            and checkpoint.get("inputSha256") == input_sha256
            and checkpoint.get("status") == stage_result.get("status")
            and checkpoint.get("outputSha256") == stage_result.get("outputSha256")
        )

    @staticmethod
    def _verified_receipt(
        *,
        identity: ApplicationIdentity,
        input_sha256: str,
        workspace: Path,
    ) -> Mapping[str, Any]:
        receipt = _read_json(workspace / "adapter-receipt.json")
        if receipt.get("identity") != identity.as_dict() or receipt.get("inputSha256") != input_sha256:
            raise PipelineAdapterError("core_collection_receipt_identity_mismatch")
        manifest_path = workspace / "phase0_reports" / "latest-manifest.json"
        quality_path = workspace / "phase0_reports" / "latest-quality-summary.json"
        if receipt.get("manifestSha256") != _sha256_file(manifest_path):
            raise PipelineAdapterError("core_collection_manifest_hash_mismatch")
        if receipt.get("qualitySha256") != _sha256_file(quality_path):
            raise PipelineAdapterError("core_collection_quality_hash_mismatch")
        result = receipt.get("stageResult")
        if not isinstance(result, Mapping):
            raise PipelineAdapterError("core_collection_receipt_invalid")
        return result


__all__ = ["BoundedOfficialSession", "CoreCollectionStageAdapter", "PipelineAdapterError"]
