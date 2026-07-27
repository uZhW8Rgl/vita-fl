from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cbor2
from nacl.signing import SigningKey

from agent.tee_inference_client import (
    TDX_REPORTDATA_OFFSET,
    TDX_RTMR3_OFFSET,
    TeeInferenceVerificationError,
    _prepare_model,
    fetch_latest_verified_tee_model_bundle,
    generate_random_tee_chestmnist_image,
    resolve_tee_inference_url,
    run_and_verify_tee_inference,
    verify_tee_inference_bundle,
)
from tee_inference.air.v1 import AirClaims, emit_receipt
from tee_inference.protocol.v1 import build_response, decode_manifest, encode_deterministic

ROOT = Path(__file__).resolve().parents[2]
VECTOR = ROOT / "tee_inference" / "vectors" / "v1-chestmnist.json"
IMAGE_DIGEST = "sha256:" + "cf" * 32
GM_STORAGE_ADDRESS = "0x" + "11" * 20
DEVICE_REGISTRY_ADDRESS = "0x" + "33" * 20
CHAIN_ID = 31337
RUNTIME_RPC_URL = "https://runtime-8545.example"
CONTRACT_POLICY_ENV = {
    "EXPECTED_GM_STORAGE_ADDRESS": GM_STORAGE_ADDRESS,
    "EXPECTED_DEVICE_REGISTRY_ADDRESS": DEVICE_REGISTRY_ADDRESS,
    "EXPECTED_CHAIN_ID": str(CHAIN_ID),
    "EXPECTED_RUNTIME_RPC_URL": RUNTIME_RPC_URL,
}


class _JsonResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self._body = json.dumps(value).encode()

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _Headers:
    @staticmethod
    def get_content_type() -> str:
        return "application/cbor"


class _CborResponse(_JsonResponse):
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers = _Headers()


class TeeInferenceEndpointTests(unittest.TestCase):
    def test_explicit_https_endpoint_wins(self) -> None:
        self.assertEqual(
            resolve_tee_inference_url("https://worker0-8080.example/"),
            "https://worker0-8080.example",
        )

    def test_endpoint_is_discovered_from_registered_w0_identity(self) -> None:
        environment = {
            "TEE_INFERENCE_URL": "",
            "EXPECTED_RUNTIME_RPC_URL": "https://runtime-8545.example",
            "EXPECTED_DEVICE_REGISTRY_ADDRESS": DEVICE_REGISTRY_ADDRESS,
            "ACCOUNT_ADDRESS": "0x" + "44" * 20,
        }
        with (
            patch.dict("os.environ", environment, clear=False),
            patch(
                "agent.blockchain_source.read_device_record",
                return_value={"public_ip": "https://worker0-8080.example"},
            ) as read_device,
        ):
            endpoint = resolve_tee_inference_url()

        self.assertEqual(endpoint, "https://worker0-8080.example")
        read_device.assert_called_once_with(
            "https://runtime-8545.example",
            DEVICE_REGISTRY_ADDRESS,
            "0x" + "44" * 20,
        )

    def test_non_https_registered_endpoint_is_rejected(self) -> None:
        environment = {
            "TEE_INFERENCE_URL": "",
            "EXPECTED_RUNTIME_RPC_URL": "https://runtime-8545.example",
            "EXPECTED_DEVICE_REGISTRY_ADDRESS": DEVICE_REGISTRY_ADDRESS,
            "ACCOUNT_ADDRESS": "0x" + "44" * 20,
        }
        with (
            patch.dict("os.environ", environment, clear=False),
            patch(
                "agent.blockchain_source.read_device_record",
                return_value={"public_ip": "http://worker0.example"},
            ),
            self.assertRaisesRegex(TeeInferenceVerificationError, "HTTPS"),
        ):
            resolve_tee_inference_url()


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
    manifest[5] = {
        **manifest[5],
        1: CHAIN_ID,
        2: bytes.fromhex(GM_STORAGE_ADDRESS[2:]),
    }
    manifest_bytes = encode_deterministic(manifest)
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
        {
            "docker_compose_file": (
                "services:\n"
                "  tee-inference:\n"
                f"    image: ghcr.io/example/tee@{IMAGE_DIGEST}\n"
                "    environment:\n"
                f'      EXPECTED_GM_STORAGE_ADDRESS: "{GM_STORAGE_ADDRESS}"\n'
                f'      EXPECTED_DEVICE_REGISTRY_ADDRESS: "{DEVICE_REGISTRY_ADDRESS}"\n'
                f'      EXPECTED_CHAIN_ID: "{CHAIN_ID}"\n'
                f'      RPC_URL: "{RUNTIME_RPC_URL}"\n'
            )
        },
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
    def setUp(self) -> None:
        self.contract_policy = patch.dict(
            "os.environ",
            CONTRACT_POLICY_ENV,
            clear=False,
        )
        self.contract_policy.start()

    def tearDown(self) -> None:
        self.contract_policy.stop()

    def test_combined_tee_tool_verifies_and_transparency_logs_job(self) -> None:
        request, bundle = valid_fixture()
        job_id = "ab" * 16
        manifest_hash = hashlib.sha256(cbor2.loads(bundle)[9]).hexdigest()
        metadata = _JsonResponse(
            {
                "ok": True,
                "job_id": job_id,
                "source_index": 3,
                "ground_truth": ["mass"],
                "manifest_sha256": manifest_hash,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "agent.tee_inference_client.urllib.request.urlopen",
                    side_effect=[metadata, _CborResponse(bundle)],
                ),
                patch(
                    "agent.scitt_client.register_verified_evidence",
                    return_value={
                        "evidence_sha256": hashlib.sha256(bundle).hexdigest(),
                        "status": "registered-and-receipt-verified",
                        "_signed_statement": b"signed statement",
                        "_transparent_statement": b"transparent statement",
                    },
                ),
                patch(
                    "agent.transparency_index.record_transparency_entry",
                    return_value={"record_id": "11" * 32},
                ),
            ):
                result = run_and_verify_tee_inference(
                    job_id,
                    endpoint="https://tee.example",
                    expected_image_digest=IMAGE_DIGEST,
                    evidence_path=str(Path(directory) / "latest-evidence.cbor"),
                )
        self.assertEqual(cbor2.loads(bundle)[2], request)
        self.assertTrue(result["ok"])
        self.assertEqual(result["job_id"], job_id)
        self.assertEqual(result["sample_index"], 3)
        self.assertEqual(result["ground_truth"], ["mass"])
        self.assertEqual(result["transparency_log"]["status"], "registered-and-receipt-verified")
        self.assertNotIn("_signed_statement", result["transparency_log"])
        self.assertNotIn("_transparent_statement", result["transparency_log"])
        self.assertEqual(result["transparency_record_id"], "11" * 32)

    def test_model_and_image_tools_use_job_api_without_paths(self) -> None:
        responses = [
            _JsonResponse(
                {
                    "ok": True,
                    "status": "ok",
                    "model_loaded": True,
                    "model_sha256": "11" * 32,
                    "manifest_sha256": "22" * 32,
                }
            ),
            _JsonResponse(
                {
                    "ok": True,
                    "job_id": "ab" * 16,
                    "source_index": 7,
                    "ground_truth": ["mass"],
                    "manifest_sha256": "22" * 32,
                }
            ),
        ]
        with patch("agent.tee_inference_client.urllib.request.urlopen", side_effect=responses) as urlopen:
            model = fetch_latest_verified_tee_model_bundle(endpoint="https://tee.example")
            job = generate_random_tee_chestmnist_image(index=7, endpoint="https://tee.example")
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(requests[0].full_url, "https://tee.example/v1/models/fetch")
        self.assertEqual(requests[1].full_url, "https://tee.example/v1/jobs")
        self.assertNotIn("path", model)
        self.assertEqual(job["job_id"], "ab" * 16)

    def test_agent_prepares_current_model_within_tool_call(self) -> None:
        manifest_hash = "12" * 32
        response = _JsonResponse({"status": "ok", "model_loaded": True, "manifest_sha256": manifest_hash})
        with patch("agent.tee_inference_client.urllib.request.urlopen", return_value=response) as urlopen:
            result = _prepare_model("https://tee.example", 30)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://tee.example/v1/prepare")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(result["manifest_sha256"], manifest_hash)

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

    def test_model_manifest_gm_storage_substitution_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        with patch.dict(
            "os.environ",
            {"EXPECTED_GM_STORAGE_ADDRESS": "0x" + "44" * 20},
            clear=False,
        ):
            with self.assertRaisesRegex(TeeInferenceVerificationError, "manifest GMStorage"):
                verify_tee_inference_bundle(bundle, request, IMAGE_DIGEST)

    def test_model_manifest_chain_substitution_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        with patch.dict(
            "os.environ",
            {"EXPECTED_CHAIN_ID": "1"},
            clear=False,
        ):
            with self.assertRaisesRegex(TeeInferenceVerificationError, "manifest chain ID"):
                verify_tee_inference_bundle(bundle, request, IMAGE_DIGEST)

    def test_measured_registry_pin_substitution_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        with patch.dict(
            "os.environ",
            {"EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "44" * 20},
            clear=False,
        ):
            with self.assertRaisesRegex(TeeInferenceVerificationError, "Compose DeviceRegistry"):
                verify_tee_inference_bundle(bundle, request, IMAGE_DIGEST)

    def test_measured_runtime_rpc_endpoint_substitution_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        with patch.dict(
            "os.environ",
            {"EXPECTED_RUNTIME_RPC_URL": "https://another-runtime-8545.example"},
            clear=False,
        ):
            with self.assertRaisesRegex(TeeInferenceVerificationError, "runtime RPC endpoint"):
                verify_tee_inference_bundle(bundle, request, IMAGE_DIGEST)

    def test_missing_local_contract_policy_is_rejected(self) -> None:
        request, bundle = valid_fixture()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(
                TeeInferenceVerificationError,
                "EXPECTED_GM_STORAGE_ADDRESS",
            ):
                verify_tee_inference_bundle(bundle, request, IMAGE_DIGEST)

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
