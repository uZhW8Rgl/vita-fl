from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import cbor2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from nacl.signing import SigningKey

from tee_inference.air.v1 import AirPolicy, verify_receipt
from tee_inference.service.attestation import AirEvidenceEmitter
from tee_inference.service.model_source import (
    _assert_w0_authorized,
    _contracts_manifest,
    _private_key_pem_from_environment,
    _verified_contract_trust_root,
    provision_latest_model,
)

ROOT = Path(__file__).resolve().parents[2]
VECTOR = ROOT / "tee_inference" / "vectors" / "v1-chestmnist.json"
MODEL = ROOT / "agent" / "downloads" / "onchain-28945426c804-aggregated.bin"


def private_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class ModelAuthorizationTests(unittest.TestCase):
    GM_STORAGE = "0x" + "11" * 20
    REGISTRY = "0x" + "33" * 20
    TRUST_ROOT_ENV = {
        "EXPECTED_GM_STORAGE_ADDRESS": GM_STORAGE,
        "EXPECTED_DEVICE_REGISTRY_ADDRESS": REGISTRY,
        "EXPECTED_CHAIN_ID": "31337",
        "EXPECTED_RUNTIME_RPC_URL": "http://rpc",
    }

    def test_contract_manifest_retries_transient_gateway_failure(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {"gm_storage_address": "0x" + "11" * 20, "registry_address": "0x" + "22" * 20}
        ).encode()
        env = {"RUNTIME_MANIFEST_TIMEOUT_SECONDS": "5", "RUNTIME_MANIFEST_RETRY_SECONDS": "0.1"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch(
                "tee_inference.service.model_source.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("temporary TLS EOF"), response],
            ) as urlopen,
            patch("tee_inference.service.model_source.time.sleep"),
        ):
            manifest = _contracts_manifest("https://runtime-5001.example")
        self.assertEqual(manifest["gm_storage_address"], "0x" + "11" * 20)
        self.assertEqual(urlopen.call_count, 2)

    def test_accepts_manifest_and_rpc_matching_measured_trust_root(self) -> None:
        contracts = {
            "gm_storage_address": self.GM_STORAGE.upper().replace("0X", "0x"),
            "registry_address": self.REGISTRY.upper().replace("0X", "0x"),
        }
        with (
            patch.dict("os.environ", self.TRUST_ROOT_ENV, clear=True),
            patch(
                "tee_inference.service.model_source.rpc_call",
                return_value=hex(31337),
            ),
        ):
            gm_storage, registry, chain_id = _verified_contract_trust_root(
                "http://rpc",
                contracts,
            )
        self.assertEqual(gm_storage, self.GM_STORAGE)
        self.assertEqual(registry, self.REGISTRY)
        self.assertEqual(chain_id, 31337)

    def test_rejects_runtime_contract_address_mismatch_before_rpc_access(self) -> None:
        contracts = {
            "gm_storage_address": "0x" + "44" * 20,
            "registry_address": self.REGISTRY,
        }
        with (
            patch.dict("os.environ", self.TRUST_ROOT_ENV, clear=True),
            patch(
                "tee_inference.service.model_source.rpc_call",
            ) as rpc,
        ):
            with self.assertRaisesRegex(RuntimeError, "GMStorage address"):
                _verified_contract_trust_root("http://rpc", contracts)
        rpc.assert_not_called()

    def test_rejects_rpc_chain_id_mismatch(self) -> None:
        contracts = {
            "gm_storage_address": self.GM_STORAGE,
            "registry_address": self.REGISTRY,
        }
        with (
            patch.dict("os.environ", self.TRUST_ROOT_ENV, clear=True),
            patch(
                "tee_inference.service.model_source.rpc_call",
                return_value=hex(1),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "RPC chain ID"):
                _verified_contract_trust_root("http://rpc", contracts)

    def test_rejects_runtime_rpc_endpoint_mismatch(self) -> None:
        contracts = {
            "gm_storage_address": self.GM_STORAGE,
            "registry_address": self.REGISTRY,
        }
        with (
            patch.dict("os.environ", self.TRUST_ROOT_ENV, clear=True),
            patch(
                "tee_inference.service.model_source.rpc_call",
            ) as rpc,
        ):
            with self.assertRaisesRegex(RuntimeError, "compose-measured runtime endpoint"):
                _verified_contract_trust_root("http://another-runtime", contracts)
        rpc.assert_not_called()

    def test_rejects_missing_measured_trust_root_configuration(self) -> None:
        contracts = {
            "gm_storage_address": self.GM_STORAGE,
            "registry_address": self.REGISTRY,
        }
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "tee_inference.service.model_source.rpc_call",
            ) as rpc,
        ):
            with self.assertRaisesRegex(RuntimeError, "EXPECTED_GM_STORAGE_ADDRESS"):
                _verified_contract_trust_root("http://rpc", contracts)
        rpc.assert_not_called()

    def test_rejects_substituted_manifest_before_registry_or_model_access(self) -> None:
        env = {
            "RPC_URL": "http://rpc",
            "KUBO_API": "http://ipfs",
            "ACCOUNT_ADDRESS": "0x" + "22" * 20,
            "RSA_PRIVATE_KEY": "not-read-before-trust-root-verification",
            **self.TRUST_ROOT_ENV,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                env,
                clear=False,
            ),
            patch(
                "tee_inference.service.model_source._contracts_manifest",
                return_value={
                    "gm_storage_address": "0x" + "44" * 20,
                    "registry_address": self.REGISTRY,
                },
            ),
            patch(
                "tee_inference.service.model_source.rpc_call",
            ) as rpc,
            patch(
                "tee_inference.service.model_source._assert_w0_authorized",
            ) as authorize,
            patch(
                "tee_inference.service.model_source.read_current_bundle_from_contract",
            ) as read_bundle,
        ):
            with self.assertRaisesRegex(RuntimeError, "GMStorage address"):
                provision_latest_model(Path(directory))
        rpc.assert_not_called()
        authorize.assert_not_called()
        read_bundle.assert_not_called()

    def test_rejects_w0_private_key_not_matching_registry(self) -> None:
        registered = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        supplied = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        registered_der = registered.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        with patch("tee_inference.service.model_source.read_device_public_key_der", return_value=registered_der):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                _assert_w0_authorized("http://rpc", "0x" + "11" * 20, "0x" + "22" * 20, private_pem(supplied))

    def test_private_key_file_takes_precedence_over_inline_environment(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        expected = private_pem(key).encode()
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "participant-private.pem"
            key_path.write_bytes(expected)
            with patch.dict(
                "os.environ",
                {
                    "RSA_PRIVATE_KEY_FILE": str(key_path),
                    "RSA_PRIVATE_KEY": "invalid-inline-key",
                },
                clear=False,
            ):
                self.assertEqual(_private_key_pem_from_environment(), expected)

    def test_private_key_loader_rejects_missing_sources(self) -> None:
        with patch.dict(
            "os.environ",
            {"RSA_PRIVATE_KEY_FILE": "", "RSA_PRIVATE_KEY": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "RSA_PRIVATE_KEY_FILE or RSA_PRIVATE_KEY"):
                _private_key_pem_from_environment()

    @unittest.skipUnless(MODEL.exists(), "native model fixture unavailable")
    def test_rejects_failed_aggregator_signature(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        env = {
            "RPC_URL": "http://rpc",
            "KUBO_API": "http://ipfs",
            "ACCOUNT_ADDRESS": "0x" + "22" * 20,
            "RSA_PRIVATE_KEY": private_pem(key),
            **self.TRUST_ROOT_ENV,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", env, clear=False),
            patch(
                "tee_inference.service.model_source._contracts_manifest",
                return_value={"gm_storage_address": "0x" + "11" * 20, "registry_address": "0x" + "33" * 20},
            ),
            patch("tee_inference.service.model_source._assert_w0_authorized"),
            patch(
                "tee_inference.service.model_source.read_current_bundle_from_contract",
                return_value={
                    "model_cid": "bafy-model",
                    "signature_cid": "bafy-sig",
                    "key_bundle_cid": "bafy-key",
                    "last_aggregator": "0x" + "44" * 20,
                    "finalized_model_round": 4,
                    "gm_storage_address": "0x" + "11" * 20,
                },
            ),
            patch(
                "tee_inference.service.model_source.fetch_onchain_bundle",
                return_value={"model_path": str(MODEL), "decryption": {"round": 4}},
            ),
            patch(
                "tee_inference.service.model_source.verify_download_with_registry",
                return_value={"ok": False},
            ),
            patch("tee_inference.service.model_source.rpc_call", return_value=hex(31337)),
        ):
            with self.assertRaisesRegex(RuntimeError, "signature"):
                provision_latest_model(Path(directory))

    def test_rejects_key_bundle_round_not_matching_finalized_contract_view(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        env = {
            "RPC_URL": "http://rpc",
            "KUBO_API": "http://ipfs",
            "ACCOUNT_ADDRESS": "0x" + "22" * 20,
            "RSA_PRIVATE_KEY": private_pem(key),
            **self.TRUST_ROOT_ENV,
        }
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "model.bin"
            model_path.write_bytes(b"model")
            with (
                patch.dict("os.environ", env, clear=False),
                patch(
                    "tee_inference.service.model_source._contracts_manifest",
                    return_value={"gm_storage_address": "0x" + "11" * 20, "registry_address": "0x" + "33" * 20},
                ),
                patch("tee_inference.service.model_source._assert_w0_authorized"),
                patch(
                    "tee_inference.service.model_source.read_current_bundle_from_contract",
                    return_value={
                        "model_cid": "bafy-model",
                        "signature_cid": "bafy-sig",
                        "key_bundle_cid": "bafy-key",
                        "last_aggregator": "0x" + "44" * 20,
                        "finalized_model_round": 5,
                        "gm_storage_address": "0x" + "11" * 20,
                    },
                ),
                patch(
                    "tee_inference.service.model_source.fetch_onchain_bundle",
                    return_value={"model_path": str(model_path), "decryption": {"round": 4}},
                ),
                patch(
                    "tee_inference.service.model_source.verify_download_with_registry",
                    return_value={"ok": True},
                ),
                patch(
                    "tee_inference.service.model_source.rpc_call",
                    return_value=hex(31337),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "finalized model round"):
                    provision_latest_model(Path(directory))


class FakeDstack:
    def call(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        if path == "/GetQuote":
            return {"quote": "aa" * 512, "event_log": "[]", "report_data": payload["report_data"]}
        if path == "/Info":
            tcb = {
                "mrtd": "10" * 48,
                "rtmr0": "20" * 48,
                "rtmr1": "30" * 48,
                "rtmr2": "40" * 48,
                "rtmr3": "50" * 48,
                "app_compose": '{"docker_compose_file":"services: {}"}',
            }
            return {"tcb_info": json.dumps(tcb)}
        raise AssertionError(path)


class AttestedBundleTests(unittest.TestCase):
    def test_bundle_binds_air_key_manifest_and_request_to_reportdata(self) -> None:
        vector = json.loads(VECTOR.read_text(encoding="utf-8"))
        manifest = bytes.fromhex(vector["manifest"]["deterministic_cbor_hex"])
        request = bytes.fromhex(vector["request"]["deterministic_cbor_hex"])
        response = bytes.fromhex(vector["response"]["deterministic_cbor_hex"])
        emitter = AirEvidenceEmitter.__new__(AirEvidenceEmitter)
        emitter.manifest_bytes = manifest
        emitter.manifest = cbor2.loads(manifest)
        emitter.manifest_hash = hashlib.sha256(manifest).digest()
        emitter.client = FakeDstack()
        emitter.signing_key = SigningKey(bytes.fromhex("2a" * 32))
        emitter.sequence = 0

        encoded = emitter.emit(request, response)
        bundle = cbor2.loads(encoded)
        expected_identity = hashlib.sha256(
            b"MasterThesis.AIR.key.v1" + bundle[5] + hashlib.sha256(manifest).digest()
        ).digest()
        self.assertEqual(bundle[10], expected_identity + hashlib.sha256(request).digest())
        claims = verify_receipt(
            bundle[4],
            bundle[5],
            AirPolicy(
                expected_nonce=cbor2.loads(request)[5],
                expected_model_hash=hashlib.sha256(manifest).digest(),
                expected_request_hash=hashlib.sha256(request).digest(),
                expected_response_hash=hashlib.sha256(response).digest(),
                expected_platform="tdx-mrtd-rtmr",
            ),
        )
        self.assertEqual(claims[-65542], hashlib.sha256(bundle[6]).digest())


if __name__ == "__main__":
    unittest.main()
