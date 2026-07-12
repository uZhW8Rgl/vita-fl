"""Canonical byte-level primitives for the ChestMNIST AIR profile v1."""

from __future__ import annotations

import hashlib
import io
import struct
from collections.abc import Mapping, Sequence

import cbor2

LABELS = (
    "atelectasis",
    "cardiomegaly",
    "effusion",
    "infiltration",
    "mass",
    "nodule",
    "pneumonia",
    "pneumothorax",
    "consolidation",
    "edema",
    "emphysema",
    "fibrosis",
    "pleural",
    "hernia",
)


class ProtocolError(ValueError):
    """Invalid or non-canonical inference protocol object."""


def _head(major: int, value: int) -> bytes:
    if value < 0:
        raise ValueError("CBOR head value must be non-negative")
    prefix = major << 5
    if value < 24:
        return bytes((prefix | value,))
    if value <= 0xFF:
        return bytes((prefix | 24, value))
    if value <= 0xFFFF:
        return bytes((prefix | 25,)) + value.to_bytes(2, "big")
    if value <= 0xFFFFFFFF:
        return bytes((prefix | 26,)) + value.to_bytes(4, "big")
    if value <= 0xFFFFFFFFFFFFFFFF:
        return bytes((prefix | 27,)) + value.to_bytes(8, "big")
    raise ValueError("integer exceeds CBOR uint64")


def encode_deterministic(value: object) -> bytes:
    """Encode the protocol's integer/bytes/text/array/map CBOR subset.

    Maps follow RFC 8949 deterministic ordering: first by encoded-key length,
    then by bytewise lexical order. Floats are intentionally unsupported.
    """

    if isinstance(value, bool) or value is None:
        raise TypeError("booleans and null are not part of this protocol")
    if isinstance(value, int):
        if value >= 0:
            return _head(0, value)
        return _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _head(3, len(raw)) + raw
    if isinstance(value, Mapping):
        encoded_items: list[tuple[bytes, bytes]] = []
        for key, item in value.items():
            encoded_key = encode_deterministic(key)
            encoded_items.append((encoded_key, encode_deterministic(item)))
        encoded_items.sort(key=lambda pair: (len(pair[0]), pair[0]))
        return _head(5, len(encoded_items)) + b"".join(
            key + item for key, item in encoded_items
        )
    if isinstance(value, Sequence):
        items = [encode_deterministic(item) for item in value]
        return _head(4, len(items)) + b"".join(items)
    raise TypeError(f"unsupported CBOR value: {type(value).__name__}")


def _sha256_cbor(value: Mapping[int, object]) -> bytes:
    return hashlib.sha256(encode_deterministic(value)).digest()


def manifest_hash(manifest: Mapping[int, object]) -> bytes:
    return _sha256_cbor(manifest)


def request_hash(request: Mapping[int, object]) -> bytes:
    return _sha256_cbor(request)


def response_hash(response: Mapping[int, object]) -> bytes:
    return _sha256_cbor(response)


def _decode_exact(raw: bytes) -> object:
    stream = io.BytesIO(raw)
    try:
        value = cbor2.CBORDecoder(stream).decode()
    except Exception as exc:
        raise ProtocolError(f"invalid CBOR: {exc}") from exc
    if stream.read(1):
        raise ProtocolError("trailing CBOR bytes")
    if encode_deterministic(value) != raw:
        raise ProtocolError("CBOR must use deterministic encoding without duplicate keys")
    return value


def _closed_map(value: object, keys: set[int], name: str) -> dict[int, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProtocolError(f"{name} must contain exactly keys {sorted(keys)}")
    if any(isinstance(key, bool) or not isinstance(key, int) for key in value):
        raise ProtocolError(f"{name} keys must be integers")
    return value


def _bytes_field(value: object, length: int, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != length:
        raise ProtocolError(f"{name} must be exactly {length} bytes")
    return value


def _uint_field(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"{name} must be an unsigned integer")
    return value


def decode_request(raw: bytes) -> dict[int, object]:
    """Decode and strictly validate a canonical v1 inference request."""

    request = _closed_map(_decode_exact(raw), {1, 2, 3, 4, 5, 6}, "request")
    if request[1] != 1:
        raise ProtocolError("unsupported request version")
    _bytes_field(request[2], 16, "request id")
    _bytes_field(request[3], 32, "manifest hash")
    _bytes_field(request[4], 784, "pixel tensor")
    _bytes_field(request[5], 32, "client nonce")
    _uint_field(request[6], "timestamp")
    return request


def decode_manifest(raw: bytes) -> dict[int, object]:
    """Decode the closed v1 model manifest and enforce its runtime contract."""

    manifest = _closed_map(_decode_exact(raw), set(range(1, 10)), "manifest")
    if manifest[1] != 1:
        raise ProtocolError("unsupported manifest version")
    if not isinstance(manifest[2], str) or not manifest[2]:
        raise ProtocolError("model id must be non-empty text")
    if not isinstance(manifest[3], str) or not manifest[3]:
        raise ProtocolError("model version must be non-empty text")
    artifact = _closed_map(manifest[4], set(range(1, 6)), "artifact")
    if artifact[1] != "model_logits.onnx" or artifact[2] != "application/onnx":
        raise ProtocolError("unsupported model artifact")
    _bytes_field(artifact[3], 32, "ONNX artifact hash")
    _uint_field(artifact[4], "ONNX artifact size")
    if artifact[5] != 18:
        raise ProtocolError("unsupported ONNX opset")
    _closed_map(manifest[5], set(range(1, 6)), "provenance")
    tensor = _closed_map(manifest[6], set(range(1, 8)), "tensor contract")
    if tensor != {
        1: "input",
        2: "float32",
        3: [1, 784],
        4: "logits",
        5: "float32",
        6: [1, 14],
        7: "row-major",
    }:
        raise ProtocolError("unsupported tensor contract")
    preprocessing = _closed_map(manifest[7], set(range(1, 6)), "preprocessing")
    if preprocessing != {
        1: "uint8-grayscale",
        2: [28, 28, 1],
        3: "row-major-hwc",
        4: "(float64(pixel)-127.5)/127.5",
        5: "float32-before-onnx",
    }:
        raise ProtocolError("unsupported preprocessing contract")
    decision = _closed_map(manifest[8], set(range(1, 5)), "decision rule")
    if decision != {1: "multilabel", 2: "sigmoid", 3: "greater-than-or-equal", 4: 500_000}:
        raise ProtocolError("unsupported decision rule")
    if manifest[9] != list(LABELS):
        raise ProtocolError("unexpected ChestMNIST label order")
    return manifest


def build_response(
    request_id: bytes,
    exact_request: bytes,
    model_manifest_hash: bytes,
    logits: Sequence[float],
    probabilities: Sequence[float],
    decisions: bytes,
    duration_microseconds: int,
) -> dict[int, object]:
    """Construct and validate a v1 response map before canonical encoding."""

    _bytes_field(request_id, 16, "request id")
    _bytes_field(model_manifest_hash, 32, "manifest hash")
    if len(logits) != 14 or len(probabilities) != 14:
        raise ProtocolError("exactly 14 logits and probabilities are required")
    _bytes_field(decisions, 14, "decisions")
    if any(value not in (0, 1) for value in decisions):
        raise ProtocolError("decision bytes must be 0 or 1")
    _uint_field(duration_microseconds, "duration")
    return {
        1: 1,
        2: request_id,
        3: hashlib.sha256(exact_request).digest(),
        4: model_manifest_hash,
        5: struct.pack("<14f", *logits),
        6: struct.pack("<14f", *probabilities),
        7: decisions,
        8: duration_microseconds,
    }


def decode_response(raw: bytes) -> dict[int, object]:
    """Decode and strictly validate a canonical v1 inference response."""

    response = _closed_map(_decode_exact(raw), set(range(1, 9)), "response")
    if response[1] != 1:
        raise ProtocolError("unsupported response version")
    _bytes_field(response[2], 16, "request id")
    _bytes_field(response[3], 32, "request hash")
    _bytes_field(response[4], 32, "manifest hash")
    _bytes_field(response[5], 56, "logits")
    _bytes_field(response[6], 56, "probabilities")
    decisions = _bytes_field(response[7], 14, "decisions")
    if any(value not in (0, 1) for value in decisions):
        raise ProtocolError("decision bytes must be 0 or 1")
    _uint_field(response[8], "duration")
    return response
