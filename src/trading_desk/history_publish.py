from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

import requests

from .pipeline_application import retry_bounded


class _RetryableHistoryResponse(RuntimeError):
    def __init__(self, error: requests.HTTPError) -> None:
        self.error = error
        super().__init__(str(error))


class _PermanentHistoryResponse(RuntimeError):
    def __init__(self, error: requests.HTTPError) -> None:
        self.error = error
        super().__init__(str(error))


def _post_chunk(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: tuple[int, int],
    max_attempts: int = 3,
    session: Any | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    transport = session if session is not None else requests

    def attempt() -> dict[str, Any]:
        try:
            response = transport.post(url, data=body, headers=headers, timeout=timeout)
            if response.ok:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("history import did not return a JSON object")
                return result
            detail = response.text[:500].replace("\n", " ")
            error = requests.HTTPError(
                f"{response.status_code} response from history import: {detail}",
                response=response,
            )
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise _RetryableHistoryResponse(error)
            raise _PermanentHistoryResponse(error)
        except Exception as exc:
            # The live recovery transport raises a typed budget error before
            # sending a request.  Retrying it here would only burn time and
            # obscure the fail-closed decision; the caller must persist a
            # retryable checkpoint instead.
            if getattr(exc, "code", None) in {
                "live_recovery_request_budget_exceeded",
                "live_recovery_source_unavailable",
            }:
                raise
            raise

    def retry_exception(exc: BaseException) -> bool:
        if getattr(exc, "code", None) in {
            "live_recovery_request_budget_exceeded",
            "live_recovery_source_unavailable",
        }:
            return False
        return isinstance(exc, _RetryableHistoryResponse) or not isinstance(
            exc, _PermanentHistoryResponse
        )

    try:
        return retry_bounded(
            attempt,
            max_attempts=max_attempts,
            should_retry_exception=retry_exception,
            sleep=time.sleep,
        )
    except _RetryableHistoryResponse as exc:
        raise RuntimeError(f"history import chunk failed after retries: {exc.error}") from exc
    except _PermanentHistoryResponse as exc:
        raise RuntimeError(f"history import chunk failed after retries: {exc.error}") from exc
    except Exception as exc:
        raise RuntimeError(f"history import chunk failed after retries: {exc}") from exc


def publish_history_bundle(
    *,
    url: str,
    bundle: dict[str, Any],
    ingest_key: str,
    access_client_id: str,
    access_client_secret: str,
    session: Any | None = None,
    max_attempts: int = 8,
    checkpoint_only: bool = False,
    finalize_only: bool = False,
    checkpoint_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upload one history artifact at a time and publish only after all pass.

    The old implementation sent every raw and normalized source in one HTTP
    request.  This keeps the same signed payload contract but turns each
    artifact into an idempotent R2 checkpoint.  A final duplicate of the last
    artifact asks the Worker to verify the expected set and atomically publish
    the date.
    """
    if checkpoint_only and finalize_only:
        raise ValueError("checkpoint_only and finalize_only are mutually exclusive")
    if not isinstance(max_attempts, int) or max_attempts <= 0 or max_attempts > 8:
        raise ValueError("max_attempts must be between 1 and 8")
    history = bundle.get("history")
    if (checkpoint_only or finalize_only) and not isinstance(history, dict):
        raise ValueError("split history operation requires a real history bundle")
    if finalize_only and not isinstance(history.get("snapshot"), dict):
        raise ValueError("history finalization requires a snapshot")
    if finalize_only:
        checkpoint = checkpoint_bundle.get("history") if isinstance(checkpoint_bundle, dict) else None
        if not isinstance(checkpoint, dict) or checkpoint.get("finalize") is not False:
            raise ValueError("source checkpoint bundle invalid")
        for field in ("runId", "runKey", "artifacts"):
            if checkpoint.get(field) != history.get(field):
                raise ValueError(f"source checkpoint identity mismatch: {field}")
        if checkpoint.get("asOf") != history.get("asOf"):
            # Existing completeness receipts are keyed by asOf. A pending
            # dashboard retains an older complete day: replay identical facts
            # under that final envelope, without relabelling any source dates.
            snapshot = history["snapshot"]
            publication = snapshot.get("publication")
            source_anchor, final_anchor = checkpoint.get("asOf"), history.get("asOf")
            try:
                valid_dates = all(isinstance(value, str) and date.fromisoformat(value).isoformat() == value
                                  for value in (source_anchor, final_anchor))
            except ValueError:
                valid_dates = False
            if not (valid_dates and final_anchor < source_anchor and snapshot.get("asOf") == final_anchor
                    and isinstance(publication, dict) and publication.get("status") == "pending"
                    and publication.get("targetAsOf") == source_anchor):
                raise ValueError("source checkpoint identity mismatch: asOf")
            finalize_only = False
    if not isinstance(history, dict):
        # Keep the lower-level helper backwards compatible for callers/tests
        # that only exercise the transport retry contract with a placeholder
        # payload. Real collection bundles always take the chunked path below.
        return _post_chunk(
            url=url,
            payload=bundle,
            headers={
                "Authorization": f"Bearer {ingest_key}",
                "CF-Access-Client-Id": access_client_id,
                "CF-Access-Client-Secret": access_client_secret,
                "Content-Type": "application/json",
            },
            timeout=(10, 90),
            max_attempts=max_attempts,
            session=session,
        )
    artifacts = history.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("history bundle did not contain artifacts")
    expected = [
        str(artifact.get("releaseId"))
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("releaseId")
    ]
    if len(expected) != len(artifacts):
        raise ValueError("history artifact is missing releaseId")
    headers = {
        "Authorization": f"Bearer {ingest_key}",
        "CF-Access-Client-Id": access_client_id,
        "CF-Access-Client-Secret": access_client_secret,
        "Content-Type": "application/json",
    }
    # The dashboard snapshot is several megabytes and is immutable for the
    # whole run.  Sending it with every resumable artifact chunk needlessly
    # makes the Worker parse and validate the same payload 20+ times.  Keep
    # it only on the final request; the Worker validates it at the atomic
    # publish boundary.
    common = {
        key: value
        for key, value in history.items()
        # The compact adjacent-change contract is verified at the final
        # completeness boundary.  Keeping it out of every artifact chunk
        # avoids repeating the same ETF universe in each resumable request.
        if key not in {"artifacts", "snapshot", "officialEtfDailyChanges"}
    }
    last_receipt = None
    for artifact in ([] if finalize_only else artifacts):
        last_receipt = _post_chunk(
            url=url,
            payload={
                "version": bundle.get("version", 1),
                "history": {
                    **common,
                    "artifacts": [artifact],
                    "expectedArtifacts": expected,
                    "finalize": False,
                },
            },
            headers=headers,
            timeout=(10, 90),
            max_attempts=max_attempts,
            session=session,
        )

        if checkpoint_only and last_receipt.get("status") != "chunk_archived":
            raise RuntimeError("source archive checkpoint receipt invalid")

    if checkpoint_only:
        if not isinstance(last_receipt, dict) or last_receipt.get("status") != "chunk_archived":
            raise RuntimeError("source archive checkpoint receipt invalid")
        return last_receipt

    # The last artifact is intentionally idempotent.  It lets the server use
    # one stable endpoint to perform the completeness check and publication.
    return _post_chunk(
        url=url,
        payload={
            "version": bundle.get("version", 1),
            "history": {
                **common,
                "snapshot": history["snapshot"],
                **(
                    {"officialEtfDailyChanges": history["officialEtfDailyChanges"]}
                    if "officialEtfDailyChanges" in history
                    else {}
                ),
                "artifacts": [artifacts[-1]],
                "expectedArtifacts": expected,
                "finalize": True,
            },
        },
        headers=headers,
        timeout=(10, 90),
        max_attempts=max_attempts,
        session=session,
    )
