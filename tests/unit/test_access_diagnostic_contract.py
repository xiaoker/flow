import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest


def diagnostic_code():
    text = (Path(__file__).resolve().parents[2] / '.github/workflows/access-smoke.yml').read_text()
    block = text.split('      - name: Compare read-only authenticated transports', 1)[1]
    code = block.split("python3 - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0]
    return '\n'.join(line[10:] for line in code.splitlines())


@pytest.mark.parametrize('python_status,curl_status', [(404, 404), (403, 404), (404, 403), (302, 404)])
def test_actual_probe_behavior(monkeypatch, capsys, python_status, curl_status):
    calls = []
    for key in ('INGEST_API_KEY', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET'):
        monkeypatch.setenv(key, 'secret-' + key)

    def run(argv, **kwargs):
        calls.append(('curl', argv))
        assert all('secret-' not in arg for arg in argv)
        assert kwargs['input'].count('header = ') == 4
        return SimpleNamespace(returncode=0, stdout=f'HTTP/2 {curl_status}\r\nContent-Type: application/json\r\nSet-Cookie: private\r\n\r\n')

    class Opener:
        def open(self, request, timeout):
            calls.append(('python', request))
            assert request.get_method() == 'GET'
            assert request.data is None and timeout == 20
            raise HTTPError(request.full_url, python_status, 'failure',
                            {'Content-Type': 'text/html', 'Location': 'secret-location'}, None)

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr('urllib.request.build_opener', lambda handler: Opener())
    if python_status == curl_status == 404:
        exec(compile(diagnostic_code(), '<diagnostic>', 'exec'), {})
    else:
        with pytest.raises(SystemExit):
            exec(compile(diagnostic_code(), '<diagnostic>', 'exec'), {})
    output = capsys.readouterr().out
    assert 'secret-' not in output and 'private' not in output
    assert len(calls) == 2
    evidence = json.loads(output)
    assert evidence['python']['status'] == python_status
    assert evidence['curl']['status'] == curl_status


def test_diagnostic_is_bounded_read_only_and_does_not_expose_secrets():
    text = (Path(__file__).resolve().parents[2] / '.github/workflows/access-smoke.yml').read_text()
    diagnostic = text.split('      - name: Compare read-only authenticated transports', 1)[1]
    code = diagnostic.split("python3 - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0]
    code = '\n'.join(line[10:] for line in code.splitlines())
    ast.parse(code)
    assert 'method="GET"' in code
    assert 'NoRedirect' in code
    assert '--max-time' in code and 'timeout=25' in code
    assert '--retry' not in code and '--location' not in code
    assert '/dev/null' in code and 'response.close()' in code
    assert 'print(json.dumps(results' in code
    assert '.read(' not in code
    assert text.count("if: ${{ inputs.auth_diagnostic_only != true && inputs.recovery_run_key == '' }}") == 3
    assert 'renewLease' not in code
