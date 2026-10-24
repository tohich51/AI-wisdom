"""Versioned xz+base64 wire envelope for the semantic-network text.

This module is intentionally dependency free and import-path agnostic: the
same file is used by the MCP server as ``kb.mcp.graph_wire`` and by the owner
local native client (copied byte-identically as ``graph_wire.py`` next to
``native_backend.py``).  Only the Python standard library may be used here.

The envelope (``xz-base64-v1``) looks like this::

    {"_kb_wire": {"version": "xz-base64-v1",
                  "encoding": "xz+base64",
                  "size": 1234,
                  "sha256": "<hex digest of the uncompressed utf-8 bytes>",
                  "data": "<base64 of the xz stream>"}}

The envelope is only produced when it is strictly smaller than the plain
JSON text it replaces, so the transport never grows and the historical
default format stays untouched for every other caller.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import lzma
import re

WIRE_KEY = "_kb_wire"
WIRE_VERSION = "xz-base64-v1"
WIRE_ENCODING = "xz+base64"
WIRE_HEADER = "x-kb-mcp-wire"
MAX_DECODED_BYTES = 8 * 1024 * 1024
DECODE_MEM_LIMIT = 16 * 1024 * 1024
COMPRESS_PRESET = 6
DICT_SIZE = 256 * 1024
COMPRESS_FILTERS = ({"id": lzma.FILTER_LZMA2, "preset": COMPRESS_PRESET, "dict_size": DICT_SIZE},)

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class WireError(ValueError):
    """Raised when a wire envelope is malformed, truncated or unsupported."""


def requested_wire_version(ctx):
    """Return the wire version advertised by the caller, or ``None``.

    The lookup is defensive on purpose: the MCP context may carry no HTTP
    request at all (stdio transports, unit tests, other callers).
    """
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    try:
        value = getter(WIRE_HEADER)
    except Exception:  # pragma: no cover - defensive only
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def encode_text(text):
    """Return the wire envelope for ``text`` or ``text`` itself.

    The plain text is returned unchanged whenever the envelope would not be
    strictly smaller, which keeps small payloads in their historical shape.
    """
    if not isinstance(text, str):
        raise WireError("wire payload must be text")
    raw = text.encode("utf-8")
    compressed = lzma.compress(raw, format=lzma.FORMAT_XZ, filters=COMPRESS_FILTERS)
    envelope = {
        WIRE_KEY: {
            "version": WIRE_VERSION,
            "encoding": WIRE_ENCODING,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data": base64.b64encode(compressed).decode("ascii"),
        }
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded) >= len(raw):
        return text
    return encoded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WireError("duplicate wire field")
        result[key] = value
    return result


def _reject_constant(value):
    raise WireError("non-finite wire field")


def decode_text(text):
    """Return the original text carried by a wire envelope.

    Text that does not carry the reserved marker is returned unchanged.  A
    payload that carries the marker is always validated: an unknown
    version, a malformed body or a failed integrity check is an error and
    never falls back to the raw payload.
    """
    if not isinstance(text, str):
        raise WireError("wire payload must be text")
    if '"' + WIRE_KEY + '"' not in text:
        return text
    try:
        envelope = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError as exc:
        raise WireError("malformed wire envelope") from exc
    if not isinstance(envelope, dict):
        raise WireError("malformed wire envelope")
    meta = envelope.get(WIRE_KEY)
    if WIRE_KEY not in envelope:
        # The marker only occurs inside the payload itself: not an envelope.
        return text
    if not isinstance(meta, dict):
        raise WireError("malformed wire envelope")
    if meta.get("version") != WIRE_VERSION:
        raise WireError("unsupported wire version")
    if meta.get("encoding") != WIRE_ENCODING:
        raise WireError("unsupported wire encoding")
    size = meta.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        raise WireError("malformed wire envelope size")
    if size < 0 or size > MAX_DECODED_BYTES:
        raise WireError("wire envelope exceeds the decode limit")
    digest = meta.get("sha256")
    if not isinstance(digest, str) or not _HEX64.match(digest):
        raise WireError("malformed wire envelope digest")
    data = meta.get("data")
    if not isinstance(data, str):
        raise WireError("malformed wire envelope data")
    try:
        compressed = base64.b64decode(data.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise WireError("malformed wire envelope data") from exc
    raw = _decompress(compressed)
    if len(raw) != size:
        raise WireError("wire envelope size mismatch")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise WireError("wire envelope digest mismatch")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WireError("wire envelope is not valid utf-8") from exc


def _decompress(compressed):
    """Return the xz payload bytes, bounded by the decode limit."""
    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=DECODE_MEM_LIMIT)
    try:
        raw = decompressor.decompress(compressed, MAX_DECODED_BYTES + 1)
    except lzma.LZMAError as exc:
        raise WireError("malformed xz stream") from exc
    if len(raw) > MAX_DECODED_BYTES:
        raise WireError("wire envelope exceeds the decode limit")
    if not decompressor.eof:
        raise WireError("incomplete xz stream")
    if decompressor.unused_data:
        raise WireError("trailing data after the xz stream")
    return raw


def decode_result(result):
    """Expand a wire envelope inside an MCP tool result.

    The result mapping is returned unchanged unless it carries a text item
    with an envelope.  Every other key of the call result is preserved and
    error results are never rewritten.
    """
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        return result
    target = result
    content = target.get("content")
    if not isinstance(content, list):
        nested = target.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("content"), list):
            target = nested
            content = nested["content"]
        else:
            return result
    items = []
    changed = False
    for item in content:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ):
            decoded = decode_text(item["text"])
            if decoded != item["text"]:
                item = dict(item)
                item["text"] = decoded
                changed = True
        items.append(item)
    if not changed:
        return result
    updated = dict(target)
    updated["content"] = items
    if target is result:
        return updated
    outer = dict(result)
    outer["result"] = updated
    return outer
