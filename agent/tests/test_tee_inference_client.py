from __future__ import annotations

import hashlib
import json
import time
import unittest
from pathlib import Path

import cbor2
from nacl.signing import SigningKey

from agent.tee_inference_client import (
    TDX_REPORTDATA_OFFSET,
    TDX_RTMR3_OFFSET,
    TeeInferenceVerificationError,
    verify_tee_inference_bundle,
)
from tee_inference.air.v1 import AirClaims, emit_receipt
from tee_inference.protocol.v1 import build_response, decode_manifest, encode_deterministic

ROOT = Path(__file__).resolve().parents[2]
VECTOR = ROOT / "tee_inference" / "vectors" / "v1-chestmnist.json"
IMAGE_DIGEST = "sha256:" + "cf" * 32


def _extend(events: list[dict[str, object]]) -> bytes:
    state = bytes(48)
    for event in events:
        serialized = (
            int(event["event_type"]).to_bytes(4, "little")
            + b":"
            + str(event["event"]).encode()
            + b":"
            + bytes.fromhex(str(event["event_payload"]))
        )
        state = hashlib.sha384(state + hashlib.sha384(serialized).digest()).digest()
    return state


def valid_fixture() -> tuple[bytes, bytes]:
    vector = json.loads(VECTOR.read_text(encoding="utf-8"))
    manifest_bytes = bytes.fromhex(vector["manifest"]["deterministic_cbor_hex"])
    manifest = decode_manifest(manifest_bytes)
    manifest_hash = hashlib.sha256(manifest_bytes).digest()
    request = encode_deterministic(
        {
            1: 1,
            2: bytes.fromhex("00112233445566778899aabbccddeeff"),
            3: manifest_hash,
            4: bytes(range(256)) * 3 + bytes(range(16)),
            5: bytes.fromhex("a5" * 32),
            6: int(time.time() * 1000),
        }
    )
    probabilities = [0.1] * 13 + [0.9]
    response = encode_deterministic(
        build_response(
            request_id=bytes.fromhex("00112233445566778899aabbccddeeff"),
            exact_request=request,
            model_manifest_hash=manifest_hash,
            logits=[0.0] * 14,
            probabilities=probabilities,
            decisions=bytes([0] * 13 + [1]),
            duration_microseconds=2_000,
        )
    )
    app_compose = json.dumps(
        {"docker_compose_file": (f"services:\n  tee-inference:\n    image: ghcr.io/example/tee@{IMAGE_DIGEST}\n")},
        sort_keys=True,
        separators=(",", ":"),
    )
    events: list[dict[str, object]] = [
        {"imr": 3, "event_type": 0x08000001, "digest": "", "event": "system-preparing", "event_payload": ""},
        {
            "imr": 3,
            "event_type": 0x08000001,
            "digest": "",
            "event": "compose-hash",
            "event_payload": hashlib.sha256(app_compose.encode()).hexdigest(),
        },
        {"imr": 3, "event_type": 0x08000001, "digest": "", "event": "system-ready", "event_payload": ""},
    ]
    signing_key = SigningKey(bytes.fromhex("2a" * 32))
    public_key = bytes(signing_key.verify_key)
    request_hash = hashlib.sha256(request).digest()
    report_data = hashlib.sha256(b"MasterThesis.AIR.key.v1" + public_key + manifest_hash).digest() + request_hash
    quote = bytearray(700)
    quote[:2] = (4).to_bytes(2, "little")
    quote[TDX_RTMR3_OFFSET : TDX_RTMR3_OFFSET + 48] = _extend(events)
    quote[TDX_REPORTDATA_OFFSET : TDX_REPORTDATA_OFFSET + 64] = report_data
    quote_bytes = bytes(quote)
    receipt = emit_receipt(
        AirClaims(
            issuer="test",
            issued_at=int(time.time()),
            cti=bytes.fromhex("00112233445566778899aabbccddeeff"),
            nonce=bytes.fromhex("a5" * 32),
            model_id=manifest[2],
            model_version=manifest[3],
            model_hash=manifest_hash,
            request_hash=request_hash,
            response_hash=hashlib.sha256(response).digest(),
            attestation_doc_hash=hashlib.sha256(quote_bytes).digest(),
            enclave_measurements={
                "measurement_type": "tdx-mrtd-rtmr",
                "pcr0": bytes.fromhex("10" * 48),
                "pcr1": bytes.fromhex("20" * 48),
                "pcr2": bytes.fromhex("30" * 48),
                "pcr3": bytes.fromhex("40" * 48),
                "pcr4": _extend(events),
            },
            policy_version="test",
            sequence_number=1,
            execution_time_ms=2,
            memory_peak_mb=0,
            security_mode="production",
        ),
        signing_key,
    )
    bundle = encode_deterministic(
        {
            1: 1,
            2: request,
            3: response,
            4: receipt,
            5: public_key,
            6: quote_bytes,
            7: json.dumps(events, separators=(",", ":")),
            8: app_compose,
            9: manifest_bytes,
            10: report_data,
        }
    )
    return request, bundle


class TeeInferenceBundleTests(unittest.TestCase):
    def test_valid_bundle_verifies(self) -> None:
        request, bundle = valid_fixture()
        result = verify_tee_inference_bundle(bundle, request, IMAGE_DIGEST)
        self.assertEqual(result["image_reference"], f"ghcr.io/example/tee@{IMAGE_DIGEST}")
        self.assertEqual(result["rtmr3_event_count"], 3)
        self.assertEqual(result["decisions"], bytes([0] * 13 + [1]))
        self.assertNotIn("manifest", result)

    def test_wrong_image_policy_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        with self.assertRaisesRegex(TeeInferenceVerificationError, "image digest"):
            verify_tee_inference_bundle(bundle, request, "sha256:" + "00" * 32)

    def test_modified_event_log_is_rejected(self) -> None:
        request, bundle_bytes = valid_fixture()
        bundle = cbor2.loads(bundle_bytes)
        events = json.loads(bundle[7])
        events[-1]["event"] = "tampered"
        bundle[7] = json.dumps(events, separators=(",", ":"))
        with self.assertRaisesRegex(TeeInferenceVerificationError, "RTMR3"):
            verify_tee_inference_bundle(encode_deterministic(bundle), request, IMAGE_DIGEST)

    def test_request_substitution_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        changed = request[:-1] + bytes([request[-1] ^ 1])
        with self.assertRaisesRegex(TeeInferenceVerificationError, "submitted request"):
            verify_tee_inference_bundle(bundle, changed, IMAGE_DIGEST)


if __name__ == "__main__":
    unittest.main()
