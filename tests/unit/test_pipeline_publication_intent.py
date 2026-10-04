"""Local durable intent fixtures, not production acceptance evidence."""
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from trading_desk.pipeline_candidate_transport import CandidatePublicationTransport
from trading_desk.pipeline_publication import build_candidate_upload_body
from trading_desk.pipeline_publication_intent import PublicationIntentStore
from trading_desk.pipeline_runner import PipelineRunnerError
from test_pipeline_candidate_transport import args, client, Session


def store(path, initialize=False):
    source = args(); manifest = json.loads(source["manifest_bytes"])
    factory = PublicationIntentStore.initialize if initialize else PublicationIntentStore
    return factory(path, execution=CandidatePublicationTransport.execution(source["context"]),
        release_id=manifest["releaseId"], manifest_sha256=manifest["manifestSha256"])


def reference(finalize=True):
    source = args()
    return CandidatePublicationTransport._reference(build_candidate_upload_body(
        source["manifest_bytes"], source["name"], source["content"],
        execution=CandidatePublicationTransport.execution(source["context"]), finalize=finalize))


def test_commit_precedes_http_and_acknowledged_restart_has_no_http(tmp_path):
    path = tmp_path / "intent.sqlite"; intent = store(path, True)
    class CheckingSession(Session):
        def request(self, *arguments, **kwargs):
            assert store(path).lookup(reference())["status"] == "PENDING"
            return super().request(*arguments, **kwargs)
    session = CheckingSession()
    receipt = client(session, intent_store=intent).upload(**args(), finalize=True)
    restarted = Session()
    assert client(restarted, intent_store=store(path)).upload(**args(), finalize=True) == receipt
    assert not restarted.calls


def test_lost_response_restart_requires_exact_readback(tmp_path):
    path = tmp_path / "intent.sqlite"; session = Session(lost=True)
    with pytest.raises(PipelineRunnerError, match="unavailable"):
        client(session, intent_store=store(path, True)).upload(**args(), finalize=True)
    restarted = client(session, intent_store=store(path))
    assert restarted.evidence()["unconfirmedWriteOutcome"] is True
    with pytest.raises(PipelineRunnerError, match="requires_reconciliation"):
        restarted.upload(**args(), finalize=True)
    assert len(session.calls) == 1
    restarted.reconcile_final_upload(**args())
    assert [row[0] for row in session.calls] == ["POST", "GET"]
    assert store(path).lookup(reference())["status"] == "ACKNOWLEDGED"


def test_ack_failure_preserves_pending(tmp_path, monkeypatch):
    path = tmp_path / "intent.sqlite"; intent = store(path, True)
    def fail(*args):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(intent, "acknowledge", fail)
    with pytest.raises(OSError):
        client(Session(), intent_store=intent).upload(**args(), finalize=True)
    assert store(path).lookup(reference())["status"] == "PENDING"


def test_atomic_competing_instances_only_one_begins(tmp_path):
    path = tmp_path / "intent.sqlite"; store(path, True)
    def begin(_):
        try:
            store(path).begin(reference()); return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(begin, range(20))) == 1


def test_missing_corrupt_symlink_and_conflicting_identity_fail_closed(tmp_path):
    path = tmp_path / "intent.sqlite"
    with pytest.raises(ValueError, match="missing"):
        store(path)
    intent = store(path, True); intent.begin(reference())
    changed = reference(); changed["execution"] = dict(changed["execution"], leaseOwner="other")
    with pytest.raises(ValueError, match="identity"):
        intent.lookup(changed)
    link = tmp_path / "link"; link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        store(link)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE intents SET digest='bad'")
    with pytest.raises(ValueError, match="corrupt"):
        intent.lookup(reference())


def test_pending_intermediate_blocks_other_write_after_restart(tmp_path):
    path = tmp_path / "intent.sqlite"; intent = store(path, True)
    intent.begin(reference(False)); session = Session()
    with pytest.raises(ValueError, match="pending_requires_reconciliation"):
        client(session, intent_store=store(path)).upload(**args(), finalize=True)
    assert not session.calls


def test_separate_process_observes_committed_unknown_intent(tmp_path):
    path = tmp_path / "intent.sqlite"; store(path, True).begin(reference())
    ref = reference()
    code = """import json,sys
from trading_desk.pipeline_publication_intent import PublicationIntentStore
r=json.loads(sys.argv[2])
s=PublicationIntentStore(sys.argv[1],execution=r['execution'],release_id=r['releaseId'],manifest_sha256=r['manifestSha256'])
assert s.lookup(r)['status']=='PENDING'
try: s.begin(r)
except ValueError: pass
else: raise AssertionError('duplicate permission')
"""
    subprocess.run([sys.executable, "-c", code, str(path), json.dumps(ref)], check=True,
                   capture_output=True, timeout=10)


def test_capacity_and_receipt_conflict(tmp_path):
    intent = store(tmp_path / "intent.sqlite", True)
    for index in range(40):
        ref = dict(reference(), name=f"artifact{index}")
        intent.begin(ref); intent.acknowledge(ref, {"status": "fixture"})
    with pytest.raises(ValueError, match="capacity"):
        intent.begin(dict(reference(), name="overflow"))
    with pytest.raises(ValueError, match="receipt_conflict"):
        intent.acknowledge(ref, {"status": "different"})
