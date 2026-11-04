"""Bind a native rolling checkpoint after a confirmed latest activation.

One POST, no retry. An ambiguous response is UNKNOWN, never permission for a
second write. Historical/stale publications do not create a rolling binding.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from trading_desk.etf_rolling_refs import (MAX_ROLLING_OBJECT_BYTES,
    resolve_rolling_binding, unpack_rolling_checkpoint)
from trading_desk.read_models import canonical_json_bytes


def bind_checkpoint(*, manifest, activation, wire, execution, exchange, headers,
                    url="https://flow.xiaoker.com/api/v1/admin/etf-state"):
    if activation.get("status") != "activated":
        return {"status": "SKIPPED", "reason": "not_latest_activation", "workerRequests": 0}
    proof = manifest.get("rollingPublication")
    if not isinstance(proof, dict) or proof.get("status") != "READY":
        raise ValueError("rolling_publication_contract_missing")
    resolve_rolling_binding(activation, manifest)
    digest = proof.get("checkpointSha256")
    inner, _, _ = unpack_rolling_checkpoint(wire, digest)
    if inner.get("asOf") != manifest["asOf"]:
        raise ValueError("rolling_checkpoint_date_mismatch")
    body = {"releaseId": manifest["releaseId"], "manifestSha256": manifest["manifestSha256"],
        "sha256": digest, "contentBase64": base64.b64encode(wire).decode(),
        "pipelineExecution": execution}
    # The Worker independently fences the active release, producer and owner.
    reply = exchange.request("POST", url, json=body, headers=headers,
        max_bytes=2048, chunk_size=2048, max_chunks=2, deadline_seconds=20)
    if reply.status != 200:
        raise ValueError("rolling_checkpoint_binding_unconfirmed")
    value = json.loads(reply.content)
    if (value.get("status") != "checkpoint_bound" or value.get("sha256") != digest
            or value.get("releaseId") != manifest["releaseId"]
            or value.get("asOf") != manifest["asOf"]
            or value.get("manifestSha256") != manifest["manifestSha256"]):
        raise ValueError("rolling_checkpoint_binding_unconfirmed")
    return {"status": "BOUND", "sha256": digest, "workerRequests": 1,
        "d1RowsRead": None, "d1RowsWritten": 0, "r2ClassA": None,
        "r2Bytes": None, "writeUpperBound": {"r2ClassA": 2, "r2Bytes": len(wire) + 2048},
        "measurementNote": "physical read/write counters unavailable; do not infer zero"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--activation", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--lease-owner", required=True)
    parser.add_argument("--attempt-count", type=int, required=True)
    args = parser.parse_args()
    from trading_desk.bounded_http import BoundedHTTPExchange
    from trading_desk.http import HttpSettings, build_session
    from trading_desk.pipeline_entry import candidate_credentials
    manifest = json.loads(args.manifest.read_bytes())
    activation = json.loads(args.activation.read_bytes())
    with args.checkpoint.open("rb") as stream:
        wire = stream.read(MAX_ROLLING_OBJECT_BYTES + 1)
    creds = candidate_credentials(os.environ)
    headers = {"Authorization": "Bearer " + creds["ingest_key"],
        "CF-Access-Client-Id": creds["access_client_id"],
        "CF-Access-Client-Secret": creds["access_client_secret"]}
    execution = {"runKey": args.run_key, "leaseOwner": args.lease_owner,
        "attemptCount": args.attempt_count, "targetDate": manifest["asOf"],
        "producerBuildSha": manifest["producerBuildSha"]}
    try:
        result = bind_checkpoint(manifest=manifest, activation=activation, wire=wire,
            execution=execution, exchange=BoundedHTTPExchange(build_session(HttpSettings(total_retries=0))),
            headers=headers)
    except Exception:
        # Do not disclose provider error bodies/credentials or retry uncertain I/O.
        args.result.write_bytes(canonical_json_bytes({"status": "UNKNOWN",
            "reason": "checkpoint_binding_not_confirmed", "workerRequestsUpper": 1,
            "d1RowsRead": None, "r2ClassA": None, "r2Bytes": None}))
        raise SystemExit("checkpoint binding unconfirmed; no automatic retry") from None
    args.result.write_bytes(canonical_json_bytes(result))


if __name__ == "__main__":
    main()
