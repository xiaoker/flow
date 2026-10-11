"""Bounded recovery locators using existing guard/budget and immutable transport."""
import hashlib
import json
from dataclasses import replace
import pytest

from trading_desk.pipeline_checkpoint_handoff import build_checkpoint_handoff, verify_checkpoint_handoff, handoff_input_sha256
from trading_desk.pipeline_runner import PipelineRunnerError
from test_pipeline_recovery_checkpoint import setup
from test_pipeline_recovery_scope import NOW


def exported(tmp_path):
    client, args, objects, budget, _, _ = setup(tmp_path)
    ref = client.upload(**args)
    inputs = {"core_collection": args["input_sha256"]}
    data, digest = build_checkpoint_handoff(args["context"], ["core_collection"], inputs,
        {"core_collection": {"remote": ref}}, approved_plan=client._plan, now=NOW)
    return client, args, objects, budget, ref, inputs, data, digest


def test_recovery_handoff_upload_read_and_owner_independent_identity(tmp_path):
    client, args, objects, _, ref, inputs, data, digest = exported(tmp_path)
    context = args["context"]
    input_hash = handoff_input_sha256(context, ["core_collection"], inputs, client._plan, NOW)
    remote = client.upload(context=context, stage="handoff", input_sha256=input_hash, data=data, sha256=digest)
    # A second host obtains its own bounded guard and allowance. Do not raise
    # the production request ceiling to accommodate an artificial single-host
    # upload/upload/download sequence.
    destination = tmp_path / "destination"
    destination.mkdir()
    reader, reader_args, reader_objects, _, _, _ = setup(destination)
    reader_objects.objects = objects.objects
    received = reader.download(context=reader_args["context"], stage="handoff", input_sha256=input_hash, sha256=digest)
    assert received == data and "/recovery/fixture-recovery/" in remote["key"]
    successor = replace(context, lease_owner="host-b", lease_attempt_count=2)
    assert handoff_input_sha256(successor, ["core_collection"], inputs, client._plan, NOW) == input_hash
    assert verify_checkpoint_handoff(received, sha256=digest, context=successor,
        input_sha256=input_hash, approved_plan=client._plan, now=NOW) == {"core_collection": ref}
    assert [c[0] for c in objects.calls] == ["POST", "POST"]
    assert [c[0] for c in reader_objects.calls] == ["GET"]
    assert objects.calls[1][1]["params"]["handoffStages"] == "core_collection"


@pytest.mark.parametrize("mutation", ["ordinary_key", "plan", "scope", "date", "stage", "lock", "size"])
def test_recovery_handoff_conflicts_reject_before_remote_effect(tmp_path, mutation):
    client, args, objects, budget, _, inputs, data, _ = exported(tmp_path)
    value = json.loads(data)
    if mutation == "ordinary_key":
        value["references"]["core_collection"]["key"] = value["references"]["core_collection"]["key"].replace(
            f'recovery/fixture-recovery/{client._scope_sha}/', "")
    elif mutation in {"plan", "scope"}:
        value["plan"]["recovery"]["planSha256" if mutation == "plan" else "scopeSha256"] = "f" * 64
    elif mutation == "date": value["plan"]["identity"]["targetDate"] = "2026-09-28"
    elif mutation == "stage": value["plan"]["stages"] = ["normalize"]
    elif mutation == "lock": value["plan"]["dependencyLockSha256"] = "f" * 64
    else: value["references"]["core_collection"]["bytes"] = True
    data = json.dumps(value).encode(); digest = hashlib.sha256(data).hexdigest()
    calls, costs = len(objects.calls), len(budget.calls)
    with pytest.raises(PipelineRunnerError):
        client.upload(context=args["context"], stage="handoff",
            input_sha256=handoff_input_sha256(args["context"], ["core_collection"], inputs, client._plan, NOW),
            data=data, sha256=digest)
    assert len(objects.calls) == calls and len(budget.calls) == costs


def test_missing_approval_or_expired_scope_cannot_form_locator(tmp_path):
    client, args, _, _, _, inputs, _, _ = exported(tmp_path)
    with pytest.raises(ValueError):
        handoff_input_sha256(args["context"], ["core_collection"], inputs)
    with pytest.raises(ValueError, match="expired"):
        from datetime import timedelta
        handoff_input_sha256(args["context"], ["core_collection"], inputs, client._plan, NOW + timedelta(hours=1))
