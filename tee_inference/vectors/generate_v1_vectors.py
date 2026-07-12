#!/usr/bin/env python3
"""Generate the checked-in deterministic-CBOR profile vectors."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from tee_inference.protocol.v1 import LABELS, encode_deterministic

OUTPUT = Path(__file__).with_name("v1-chestmnist.json")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    artifact_digest = bytes.fromhex(
        "37f6d03679ff0af626905e27bd5908238118982fca493be84c3ef41eda0c38b4"
    )
    manifest = {
        1: 1,
        2: "master-thesis/chestmnist-dfl",
        3: "round-1",
        4: {
            1: "aggregated.bin",
            2: "application/vnd.master-thesis.dfl-model",
            3: artifact_digest,
            4: 214_640,
            5: "float64-le-v1",
        },
        5: {
            1: 31_337,
            2: bytes.fromhex("11" * 20),
            3: 1,
            4: "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3pte7jst7j7n2xg5h5example",
            5: bytes.fromhex("22" * 32),
        },
        6: {
            1: "input",
            2: "float64",
            3: [1, 784],
            4: "logits",
            5: "float64",
            6: [1, 14],
            7: "row-major",
        },
        7: {
            1: "uint8-grayscale",
            2: [28, 28, 1],
            3: "row-major-hwc",
            4: "(float64(pixel)-127.5)/127.5",
            5: "float64-native-pytorch",
        },
        8: {
            1: "multilabel",
            2: "sigmoid",
            3: "greater-than-or-equal",
            4: 500_000,
        },
        9: list(LABELS),
    }
    manifest_cbor = encode_deterministic(manifest)
    manifest_digest = hashlib.sha256(manifest_cbor).digest()

    request = {
        1: 1,
        2: bytes.fromhex("00112233445566778899aabbccddeeff"),
        3: manifest_digest,
        4: bytes(range(256)) * 3 + bytes(range(16)),
        5: bytes.fromhex("a5" * 32),
        6: 1_750_000_000_123,
    }
    request_cbor = encode_deterministic(request)
    request_digest = hashlib.sha256(request_cbor).digest()

    logits = [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0] * 2
    probabilities = [
        0.11920292,
        0.26894143,
        0.37754068,
        0.5,
        0.62245935,
        0.7310586,
        0.8807971,
    ] * 2
    response = {
        1: 1,
        2: request[2],
        3: request_digest,
        4: manifest_digest,
        5: struct.pack("<14d", *logits),
        6: struct.pack("<14d", *probabilities),
        7: bytes(int(value >= 0.5) for value in probabilities),
        8: 12_345,
    }
    response_cbor = encode_deterministic(response)

    vector = {
        "profile": "master-thesis-chestmnist-air-v1",
        "description": "Synthetic protocol vector; provenance values are fixtures, not a deployment.",
        "model_hash_scheme": "sha256-manifest",
        "manifest": {
            "deterministic_cbor_hex": manifest_cbor.hex(),
            "sha256_hex": digest(manifest_cbor),
        },
        "request": {
            "deterministic_cbor_hex": request_cbor.hex(),
            "sha256_hex": digest(request_cbor),
        },
        "response": {
            "deterministic_cbor_hex": response_cbor.hex(),
            "sha256_hex": digest(response_cbor),
        },
        "air_claim_values": {
            "-65539_model_hash": digest(manifest_cbor),
            "-65540_request_hash": digest(request_cbor),
            "-65541_response_hash": digest(response_cbor),
            "-65549_model_hash_scheme": "sha256-manifest",
        },
    }
    OUTPUT.write_text(json.dumps(vector, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
