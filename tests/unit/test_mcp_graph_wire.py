import base64
import gzip
import hashlib
import json

import pytest

from kb.mcp.graph_wire import (
    MAX_DECODED_BYTES,
    WIRE_KEY,
    WIRE_VERSION,
    WireError,
    decode_result,
    decode_text,
    encode_text,
    requested_wire_version,
)


def _wrap(raw: bytes, *, size=None, digest=None, payload=None) -> str:
    body = payload if payload is not None else gzip.compress(raw, compresslevel=3, mtime=0)
    envelope = {
        WIRE_KEY: {
            "version": WIRE_VERSION,
            "encoding": "gzip+base64",
            "size": len(raw) if size is None else size,
            "sha256": hashlib.sha256(raw).hexdigest() if digest is None else digest,
            "data": base64.b64encode(body).decode("ascii"),
        }
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def test_roundtrip_keeps_unicode_text_byte_identical() -> None:
    text = json.dumps(
        {"nodes": [{"label": "“Root” — ∑ ≤ ∞ 🧬"}], "padding": "ø" * 4096},
        ensure_ascii=False,
    )
    encoded = encode_text(text)
    assert encoded != text
    assert json.loads(encoded)[WIRE_KEY]["version"] == WIRE_VERSION
    assert decode_text(encoded) == text
    assert decode_text(encoded).encode("utf-8") == text.encode("utf-8")


def test_normal_payloads_are_left_untouched() -> None:
    small = json.dumps({"nodes": [], "edges": []})
    assert encode_text(small) == small
    assert decode_text(small) == small
    assert decode_text("plain text") == "plain text"


def test_error_results_are_never_rewritten() -> None:
    envelope = _wrap(b'{"nodes": []}')
    result = {"isError": True, "content": [{"type": "text", "text": envelope}]}
    assert decode_result(result) is result


def test_result_decoding_preserves_every_other_key() -> None:
    text = json.dumps({"padding": "y" * 4096}, separators=(",", ":"))
    encoded = encode_text(text)
    assert encoded != text
    result = {
        "content": [{"type": "text", "text": encoded, "annotations": None}],
        "isError": False,
        "structuredContent": None,
    }
    decoded = decode_result(result)
    assert decoded is not result
    assert decoded["content"][0]["text"] == text
    assert decoded["content"][0]["annotations"] is None
    assert decoded["isError"] is False
    assert "structuredContent" in decoded


def test_unknown_version_is_rejected_without_fallback() -> None:
    envelope = json.dumps({WIRE_KEY: {"version": "gzip-base64-v2", "data": "!!"}})
    with pytest.raises(WireError, match="version"):
        decode_text(envelope)


def test_oversized_decompression_is_rejected() -> None:
    raw = b"\0" * (MAX_DECODED_BYTES + 4096)
    envelope = _wrap(raw, size=16, digest="0" * 64)
    with pytest.raises(WireError, match="decode limit"):
        decode_text(envelope)


def test_declared_size_above_the_limit_is_rejected() -> None:
    raw = b"\0" * 64
    envelope = _wrap(raw, size=MAX_DECODED_BYTES + 1)
    with pytest.raises(WireError, match="decode limit"):
        decode_text(envelope)


def test_hash_mismatch_is_rejected() -> None:
    raw = b'{"nodes": []}' * 32
    envelope = _wrap(raw, digest="0" * 64)
    with pytest.raises(WireError, match="digest"):
        decode_text(envelope)


def test_trailing_data_and_truncated_stream_are_rejected() -> None:
    raw = b'{"nodes": []}'
    payload = gzip.compress(raw, compresslevel=3, mtime=0)
    with pytest.raises(WireError, match="trailing"):
        decode_text(_wrap(raw, payload=payload + b"junk"))
    with pytest.raises(WireError, match="incomplete"):
        decode_text(_wrap(raw, payload=payload[:-4]))


def test_invalid_utf8_and_malformed_fields_are_rejected() -> None:
    with pytest.raises(WireError, match="utf-8"):
        decode_text(_wrap(b"\xff\xfe\x00bad"))
    broken = json.dumps(
        {
            WIRE_KEY: {
                "version": WIRE_VERSION,
                "encoding": "gzip+base64",
                "size": True,
                "sha256": "0" * 64,
                "data": "!!!!",
            }
        }
    )
    with pytest.raises(WireError):
        decode_text(broken)


def test_header_detection_is_optional() -> None:
    assert requested_wire_version(object()) is None
    assert requested_wire_version(None) is None


@pytest.mark.parametrize(
    "text", ['{"_kb_wire":null}', '{"_kb_wire":{},"_kb_wire":{}}', '{"_kb_wire":{"size":NaN}}']
)
def test_reserved_envelopes_reject_null_duplicates_and_nonfinite(text):
    with pytest.raises(WireError):
        decode_text(text)


def test_malformed_base64_is_rejected_before_inflate():
    value = json.loads(_wrap(b"synthetic"))
    value[WIRE_KEY]["data"] = "!!!!"
    with pytest.raises(WireError, match="data"):
        decode_text(json.dumps(value))
