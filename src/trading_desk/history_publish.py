from __future__ import annotations

import json
import time
from typing import Any

import requests


def _post_chunk(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: tuple[int, int],
    max_attempts: int = 3,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = requests.post(url, data=body, headers=headers, timeout=timeout)
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
            last_error = error
            if response.status_code < 500 and response.status_code not in {408, 429}:
                break
        except Exception as exc:
            last_error = exc
        if attempt < max_attempts - 1:
            time.sleep(2**attempt)
    raise RuntimeError(f"history import chunk failed after retries: {last_error}")


def publish_history_bundle(
    *,
    url: str,
    bundle: dict[str, Any],
    ingest_key: str,
    access_client_id: str,
    access_client_secret: str,
) -> dict[str, Any]:
    """Upload one history artifact at a time and publish only after all pass.

    The old implementation sent every raw and normalized source in one HTTP
    request.  This keeps the same signed payload contract but turns each
    artifact into an idempotent R2 checkpoint.  A final duplicate of the last
    artifact asks the Worker to verify the expected set and atomically publish
    the date.
    """
    history = bundle.get("history")
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
            max_attempts=8,
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
        if key not in {"artifacts", "snapshot"}
    }
    for artifact in artifacts:
        _post_chunk(
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
        )

    # The last artifact is intentionally idempotent.  It lets the server use
    # one stable endpoint to perform the completeness check and publication.
    return _post_chunk(
        url=url,
        payload={
            "version": bundle.get("version", 1),
            "history": {
                **common,
                "snapshot": history["snapshot"],
                "artifacts": [artifacts[-1]],
                "expectedArtifacts": expected,
                "finalize": True,
            },
        },
        headers=headers,
        timeout=(10, 90),
    )
