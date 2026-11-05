import os
from pathlib import Path
import subprocess

import pytest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/szse-single-archive-staging.yml"
HOST = "https://21004860faeebc5fa4f297679796ee00.r2.cloudflarestorage.com"
DIGEST = "e06303e697a8d97fc20d54c946288947157ec09cb19cfffc794aa149fdc4be2e"
PREFIX = f"{HOST}/flow/operator-staging/szse-20261014-{DIGEST}.json?"
QUERY = "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=synthetic-test-secret"


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
    mock = 'curl() { printf "called" > "$RUNNER_TEMP/curl-called"; printf 403; }\n'
    result = subprocess.run(["bash", "-c", mock + shell], env={
        **os.environ, "RUNNER_TEMP": str(tmp_path), "PAYLOAD_SHA": DIGEST,
        "STAGING_GET_URL": url, "CONFIRM": "",
    }, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert (tmp_path / "curl-called").exists() == reaches_curl
    assert "synthetic-test-secret" not in result.stdout + result.stderr
