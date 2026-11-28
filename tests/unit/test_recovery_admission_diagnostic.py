import io
import json
import pytest
from scripts.diagnose_recovery_admission import diagnose

KEY = "daily-market-data:2026-11-27:reconcile:v1"


class Response(io.BytesIO):
    code = 200


def test_two_gets_no_secrets_or_execution_authority():
    calls = []
    class Opener:
        def open(self, request, timeout):
            assert request.method == "GET" and request.data is None and timeout == 20
            calls.append(request.full_url)
            value = ({"runKey": KEY, "status": "BLOCKED_QUALITY", "secret": "hidden"}
                     if len(calls) == 1 else {"contract": "flow-budget-diagnostic-v1",
                     "executionAllowed": False, "evidenceReady": False, "held": {"d1RowsRead": 10}})
            return Response(json.dumps(value).encode())
    result = diagnose(KEY, {"Authorization": "Bearer fixture"}, Opener())
    assert len(calls) == 2 and result["executionAllowed"] is False
    assert result["budget"]["evidenceReady"] is False
    assert "hidden" not in json.dumps(result)


@pytest.mark.parametrize("key", ["bad", "daily-market-data:2026-02-30:reconcile:v1", KEY.replace("v1", "v2")])
def test_invalid_key_before_network(key):
    with pytest.raises(ValueError):
        diagnose(key, {})


def test_response_limit():
    class Opener:
        def open(self, *args, **kwargs):
            return Response(b"x" * 262145)
    with pytest.raises(ValueError, match="response_limit"):
        diagnose(KEY, {}, Opener())


@pytest.mark.parametrize("fault", ["redirect", "unauthorized", "wrong_key", "budget_authority", "bad_json"])
def test_fail_closed(fault):
    class Opener:
        count = 0
        def open(self, *args, **kwargs):
            self.count += 1
            value = {"runKey": KEY, "status": "COMPLETED"} if self.count == 1 else {
                "contract": "flow-budget-diagnostic-v1", "executionAllowed": False}
            response = Response(json.dumps(value).encode())
            if fault in {"redirect", "unauthorized"}: response.code = 302 if fault == "redirect" else 401
            if fault == "wrong_key": response = Response(b'{"runKey":"other","status":"COMPLETED"}')
            if fault == "bad_json": response = Response(b"not-json")
            if fault == "budget_authority" and self.count == 2:
                response = Response(b'{"contract":"flow-budget-diagnostic-v1","executionAllowed":true}')
            return response
    with pytest.raises(ValueError):
        diagnose(KEY, {}, Opener())


def test_single_key_mode_skips_all_source_calls():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/access-smoke.yml").read_text()
    assert text.count("inputs.auth_diagnostic_only != true && inputs.recovery_run_key == ''") == 3
    assert "inputs.auth_diagnostic_only == true && inputs.recovery_run_key == ''" in text
    assert "DIAGNOSTIC_RUN_KEY: ${{ inputs.recovery_run_key || '' }}" in text
