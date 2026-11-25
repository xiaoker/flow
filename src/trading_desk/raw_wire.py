"""Versioned transport envelope; source bytes and their identity stay unchanged."""
import base64
import gzip
import hashlib
import io

CONTRACT = "flow-raw-gzip-wire-v1"
MAX_WIRE_BYTES = 2_000_000
MAX_SOURCE_BYTES = 4_000_000


def encode_raw_wire(source: bytes) -> dict:
    if not isinstance(source, bytes) or not 0 < len(source) <= MAX_SOURCE_BYTES:
        raise ValueError("raw_wire_source_size_invalid")
    wire = gzip.compress(source, compresslevel=6, mtime=0)
    if len(wire) > MAX_WIRE_BYTES:
        raise ValueError("raw_wire_size_exceeded")
    return {"contract": CONTRACT, "encoding": "gzip", "sourceBytes": len(source),
            "sourceSha256": hashlib.sha256(source).hexdigest(),
            "wireSha256": hashlib.sha256(wire).hexdigest(),
            "contentBase64": base64.b64encode(wire).decode("ascii")}


def decode_raw_wire(value: dict) -> bytes:
    keys = {"contract", "encoding", "sourceBytes", "sourceSha256", "wireSha256", "contentBase64"}
    if (not isinstance(value, dict) or set(value) != keys or value["contract"] != CONTRACT
            or value["encoding"] != "gzip" or type(value["sourceBytes"]) is not int
            or not 0 < value["sourceBytes"] <= MAX_SOURCE_BYTES):
        raise ValueError("raw_wire_contract_invalid")
    encoded = value["contentBase64"]
    if not isinstance(encoded, str) or len(encoded) > ((MAX_WIRE_BYTES + 2) // 3) * 4:
        raise ValueError("raw_wire_size_exceeded")
    wire = base64.b64decode(encoded, validate=True)
    if len(wire) > MAX_WIRE_BYTES or hashlib.sha256(wire).hexdigest() != value["wireSha256"]:
        raise ValueError("raw_wire_hash_mismatch")
    with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
        source = stream.read(value["sourceBytes"] + 1)
    if len(source) != value["sourceBytes"] or hashlib.sha256(source).hexdigest() != value["sourceSha256"]:
        raise ValueError("raw_wire_source_mismatch")
    return source
