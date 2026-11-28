"""Bounded GET-only observation; never grants recovery execution authority."""
import json
import os
from datetime import date
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def diagnose(run_key, headers, opener=None):
    import re
    match = re.fullmatch(r"daily-market-data:(\d{4}-\d{2}-\d{2}):(evening|morning|reconcile):v1", run_key)
    if not match or date.fromisoformat(match[1]).isoformat() != match[1]:
        raise ValueError("invalid_run_key")
    if any(any(c in value for c in "\r\n") for value in headers.values()):
        raise ValueError("invalid_credentials")
    opener = opener or build_opener(NoRedirect())
    base = "https://flow.xiaoker.com/api/v1/admin/pipeline-window/"
    results = {"executionAllowed": False}
    for name, path in (("pipeline", "status?runKey=" + quote(run_key, safe="")),
                       ("budget", "budget?operation=diagnostic_summary")):
        try:
            response = opener.open(Request(base + path, headers=headers, method="GET"), timeout=20)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError("diagnostic_response_limit")
            if response.code == 404 and name == "pipeline":
                results[name] = {"status": "NOT_FOUND"}
                continue
            if response.code != 200:
                raise ValueError("diagnostic_http_" + str(response.code))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("diagnostic_response_shape")
            fields = ("runKey", "status", "attemptCount", "updatedAt", "leaseExpiresAt", "targetDate") if name == "pipeline" else (
                "contract", "initialized", "revision", "epochDay", "epochMonth", "evidenceReady",
                "executionAllowed", "evidence", "caps", "settled", "held", "reservationStatusCounts", "rowsRead", "rowsWritten")
            if name == "budget" and (value.get("contract") != "flow-budget-diagnostic-v1" or value.get("executionAllowed") is not False):
                raise ValueError("diagnostic_budget_contract")
            if name == "pipeline" and (value.get("runKey") != run_key or value.get("status") not in {
                    "DISPATCHED", "RUNNING", "COMPLETED", "FAILED_RETRYABLE", "BLOCKED_QUALITY"}):
                raise ValueError("diagnostic_run_key_mismatch")
            results[name] = {key: value[key] for key in fields if key in value}
    return results


if __name__ == "__main__":
    headers = {"Authorization": "Bearer " + os.environ["INGEST_API_KEY"],
               "CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
               "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"]}
    print(json.dumps(diagnose(os.environ["DIAGNOSTIC_RUN_KEY"], headers), sort_keys=True))
