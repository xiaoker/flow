import os
from pathlib import Path
import subprocess
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/szse-single-archive-staging.yml"
HOST = "https://21004860faeebc5fa4f297679796ee00.r2.cloudflarestorage.com"
DIGEST = "e06303e697a8d97fc20d54c946288947157ec09cb19cfffc794aa149fdc4be2e"
PREFIX = f"{HOST}/flow/operator-staging/szse-20261014-{DIGEST}.json?"
QUERY = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=synthetic-test-secret"
MOCK_CURL = r'''curl() {
  local output=""
  while (($#)); do
    if [[ "$1" == "-o" ]]; then
      output="$2"
      shift 2
    else
      shift
    fi
  done
  printf "called" > "$RUNNER_TEMP/curl-called"
  printf "%s" "$MOCK_BODY" > "$output"
  printf "%s" "$MOCK_STATUS"
}
'''


def run_staging_step(tmp_path, url, body="", status="400"):
    source = WORKFLOW.read_text()
    step = source.split("- name: Fetch and validate one private staging object", 1)[1]
    shell = step.split("run: |\n", 1)[1].split("\n      - name:", 1)[0]
    return subprocess.run(["bash", "-c", MOCK_CURL + textwrap.dedent(shell)], env={
        **os.environ, "RUNNER_TEMP": str(tmp_path), "PAYLOAD_SHA": DIGEST,
        "STAGING_GET_URL": url, "CONFIRM": "", "MOCK_BODY": body,
        "MOCK_STATUS": status,
    }, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("url,reaches_curl", [
    ("", False),
    ("https://example.invalid/" + PREFIX + QUERY, False),
    (PREFIX.replace("/flow/", "/other/") + QUERY, False),
    (PREFIX + QUERY + '"\nurl = "https://example.invalid', False),
    (PREFIX + QUERY, True),
])
def test_staging_url_gate_before_network(tmp_path, url, reaches_curl):
    source = WORKFLOW.read_text()
    step = source.split("- name: Fetch and validate one private staging object", 1)[1]
    shell = step.split("run: |\n", 1)[1].split("\n      - name:", 1)[0]
    assert "--location" not in shell
    result = run_staging_step(
        tmp_path,
        url,
        "<Error><Code>SignatureDoesNotMatch</Code><Message>private detail</Message></Error>",
        "400",
    )
    assert result.returncode != 0
    assert (tmp_path / "curl-called").exists() == reaches_curl
    assert "synthetic-test-secret" not in result.stdout + result.stderr
    if reaches_curl:
        assert "R2 code=SignatureDoesNotMatch" in result.stderr
        assert "private detail" not in result.stdout + result.stderr


@pytest.mark.parametrize(("body", "expected"), [
    (
        "<Error><Code>InvalidAccessKeyId</Code><Message>private detail</Message></Error>",
        "InvalidAccessKeyId",
    ),
    (
        "<Error><Code>UnapprovedProviderCode</Code><Message>private detail</Message></Error>",
        "UNKNOWN",
    ),
    ("not xml", "UNKNOWN"),
])
def test_staging_http_error_logs_only_allowlisted_code(tmp_path, body, expected):
    result = run_staging_step(tmp_path, PREFIX + QUERY, body, "400")

    assert result.returncode != 0
    assert f"R2 code={expected}" in result.stderr
    assert "private detail" not in result.stdout + result.stderr
    assert "UnapprovedProviderCode" not in result.stdout + result.stderr
    assert "synthetic-test-secret" not in result.stdout + result.stderr
    assert not (tmp_path / "szse-archive.json").exists()


def test_staging_large_http_error_body_is_not_parsed(tmp_path):
    result = run_staging_step(tmp_path, PREFIX + QUERY, "x" * 65537, "400")

    assert result.returncode != 0
    assert "R2 code=ERROR_BODY_TOO_LARGE" in result.stderr
    assert "x" * 100 not in result.stdout + result.stderr
