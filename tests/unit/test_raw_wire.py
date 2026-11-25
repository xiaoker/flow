import pytest

from trading_desk.raw_wire import decode_raw_wire, encode_raw_wire


def test_large_source_roundtrip_retains_exact_identity():
    source = b"x" * 2_049_642
    encoded = encode_raw_wire(source)
    assert decode_raw_wire(encoded) == source
    assert encode_raw_wire(source) == encoded


@pytest.mark.parametrize("field,value", [("sourceBytes", 1), ("sourceSha256", "0" * 64),
                                       ("wireSha256", "0" * 64), ("encoding", "identity")])
def test_tampered_envelope_rejected(field, value):
    envelope = encode_raw_wire(b"unchanged official bytes")
    envelope[field] = value
    with pytest.raises(ValueError):
        decode_raw_wire(envelope)


def test_source_expansion_bound():
    with pytest.raises(ValueError):
        encode_raw_wire(b"x" * 4_000_001)


def test_explicit_archive_client_roundtrip(monkeypatch):
    import hashlib
    import json
    from scripts import build_phase0_archive_recipe as client
    source = b"x" * 2_049_642
    digest = hashlib.sha256(source).hexdigest()
    key = f"market-data/raw/szse/etf_scale/2026-09-14/{digest}.json"
    wire = json.dumps(encode_raw_wire(source)).encode()
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "local-fixture-only")

    class Reply:
        status = 200
        headers = {"x-flow-archive-key": key, "x-flow-archive-sha256": digest,
                   "x-flow-raw-wire-contract": "flow-raw-gzip-wire-v1"}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, maximum): return wire[:maximum]

    class Opener:
        def open(self, request, timeout):
            assert "raw_wire=flow-raw-gzip-wire-v1" in request.full_url
            assert timeout == 20
            return Reply()

    monkeypatch.setattr(client, "build_opener", lambda *_: Opener())
    assert client._archive_get(key, digest, raw_wire=True) == source
    with pytest.raises(ValueError, match="scope_invalid"):
        client._archive_get(key.replace("/szse/", "/sse/"), digest, raw_wire=True)
