import os
import hashlib
from pathlib import Path
import subprocess
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/szse-single-archive-staging.yml"
HOST = "https://21004860faeebc5fa4f297679796ee00.r2.cloudflarestorage.com"
DIGEST = "0d41e50cf184b6cad329b1de0eb5e0651660c9aacc566d30b651dc941fc329a4"
NORMALIZED_DIGEST = "68ae8f780c83d0877b105641aab991d56f5dff9c226636390bb2b11c487afc9c"
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


def test_workflow_pins_the_corrected_single_date_bundle():
    source = WORKFLOW.read_text()
    assert f"PAYLOAD_SHA: {DIGEST}" in source
    assert f"NORMALIZED_SHA: {NORMALIZED_DIGEST}" in source
    assert 'TARGET: "2026-10-14"' in source
    assert "e06303e697a8d97fc20d54c946288947157ec09cb19cfffc794aa149fdc4be2e" not in source
    assert "1b873671b4b512d403fc981878b488e11bc2b7a7d780e02db3d314705b5d832d" not in source


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


def test_verify_only_isolated_from_staging_and_import():
    source = WORKFLOW.read_text()
    assert "if: inputs.verify_only != true && inputs.confirm_apply != ''" in source
    staging = source.split("- name: Fetch and validate one private staging object", 1)[1]
    assert staging.lstrip().startswith("if: inputs.verify_only != true")
    step = source.split("- name: Verify existing production partition only", 1)[1]
    assert "if: inputs.verify_only == true" in step
    assert 'test -z "$CONFIRM"' in step
    assert "--request GET" in step
    assert "--retry" not in step and "--location" not in step
    assert "history-import" not in step and "--data" not in step
    assert "STAGING_GET_URL" not in step
    assert 'echo "$NORMALIZED_SHA  $csv" | sha256sum -c --status' in step
    assert "import completion remains unproven" in step
    shell = textwrap.dedent(step.split("run: |\n", 1)[1])
    assert subprocess.run(["bash", "-n"], input=shell, text=True).returncode == 0


def test_verify_only_rejects_apply_confirmation_before_network(tmp_path):
    source = WORKFLOW.read_text().split("- name: Verify existing production partition only", 1)[1]
    shell = textwrap.dedent(source.split("run: |\n", 1)[1])
    result = subprocess.run(["bash", "-c", MOCK_CURL + shell], env={
        **os.environ, "RUNNER_TEMP": str(tmp_path), "CONFIRM": DIGEST,
    }, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / "curl-called").exists()


@pytest.mark.parametrize("status,valid_hash,expected", [("200", True, 0), ("404", True, 1), ("200", False, 1)])
def test_verify_only_shell_reads_once_and_verifies_hash(tmp_path, status, valid_hash, expected):
    step = WORKFLOW.read_text().split("- name: Verify existing production partition only", 1)[1]
    shell = textwrap.dedent(step.split("run: |\n", 1)[1])
    mock = r'''sha256sum() {
      read -r expected file
      actual="$(shasum -a 256 "$file")"
      test "${actual%% *}" = "$expected"
    }
    curl() {
      printf '%s\n' "$*" > "$RUNNER_TEMP/request"
      while (($#)); do
        case "$1" in
          -o) printf 'fixture\n' > "$2"; shift 2;;
          -D) printf 'x-flow-trade-date: 2026-10-14\r\n' > "$2"; shift 2;;
          *) shift;;
        esac
      done
      printf '%s' "$MOCK_STATUS"
    }
    '''
    result = subprocess.run(["bash", "-c", mock + shell], env={
        **os.environ, "RUNNER_TEMP": str(tmp_path), "CONFIRM": "", "TARGET": "2026-10-14",
        "INGEST": "synthetic-secret", "ACCESS_ID": "synthetic-id", "ACCESS_SECRET": "synthetic-secret",
        "MOCK_STATUS": status,
        "NORMALIZED_SHA": hashlib.sha256(b"fixture\n").hexdigest() if valid_hash else "0" * 64,
    }, capture_output=True, text=True)
    assert (result.returncode == 0) == (expected == 0)
    request = (tmp_path / "request").read_text()
    assert "--request GET" in request and "history-import" not in request
    assert "synthetic-secret" not in result.stdout + result.stderr
    assert not (tmp_path / "szse-existing.csv").exists()
