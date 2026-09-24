from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from nacl.signing import SigningKey

from agent.sello_client import begin_receiver_call, complete_receiver_call
from agent_receipts.environment import RECEIPT_HEADER, receipt_header
from agent_receipts.receiver_log import SCITT_BUNDLE_URL_HEADER, SCITT_TRANSACTION_HEADER
from agent_receipts.sello_v1 import SelloReceiver, b64url_encode, verify_authorization_token
from transport_security.client import cert_thumbprint


class SelloClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        key = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-agent")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .sign(key, None)
        )
        path = Path(self.directory.name) / "agent.crt"
        path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path = Path(self.directory.name) / "agent.key"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        key_path.chmod(0o600)
        self.environment = patch.dict(
            os.environ, {"TLS_CERT_PATH": str(path), "TLS_KEY_PATH": str(key_path)}, clear=False
        )
        self.environment.start()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.environment.stop)

    def test_receiver_receipt_is_verified_before_scitt_indexing(self) -> None:
        issuer = SigningKey(bytes.fromhex("41" * 32))
        service = SigningKey(bytes.fromhex("42" * 32))
        environment = {
            "SELLO_REQUIRED": "1",
            "SELLO_TOKEN_ISSUER_SIGNING_SEED": b64url_encode(bytes(issuer)),
            "SELLO_OWNER_HPKE_PRIVATE_KEY": b64url_encode(bytes.fromhex("43" * 32)),
            "SELLO_SERVICE_REGISTRY": json.dumps({"zk-inference": b64url_encode(bytes.fromhex("44" * 32))}),
            "SELLO_LOG_URLS": "https://scitt.example",
            "EXPECTED_RUNTIME_RPC_URL": "https://runtime-8545.example",
            "EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "11" * 20,
            "TEE_INFERENCE_PARTICIPANT_ADDRESS": "0x" + "12" * 20,
        }
        action_input = b'{"index":0}'
        action_output = b'{"ok":true}'
        with (
            patch.dict(os.environ, environment, clear=False),
            patch(
                "agent.blockchain_source.read_sello_receiver",
                return_value={
                    "authorized": True,
                    "public_ip": "https://worker0-8080.example",
                    "sello_receipt_key": bytes(service.verify_key),
                },
            ) as read_receiver,
        ):
            call = begin_receiver_call(
                "generate_random_tee_chestmnist_image",
                "tee-inference",
                action_input,
                receiver_base_url="https://worker0-8080.example",
            )
            self.assertIsNotNone(call)
            self.assertEqual(call.trusted_service_key, bytes(service.verify_key))
            claims = verify_authorization_token(
                call.token,
                issuer.verify_key,
                expected_audience="https://worker0-8080.example",
                required_scope="generate_random_tee_chestmnist_image",
                cert_thumbprint=cert_thumbprint(),
            )
            self.assertIn("receipts:read", claims["scope"])
            self.assertLessEqual(claims["exp"] - claims["iat"], 300)
            receiver = SelloReceiver("tee-inference", service, issuer.verify_key)
            envelope = receiver.issue(
                call.token,
                action_type=call.action,
                action_input=action_input,
                action_output=action_output,
                result_status="success",
            )
            transparency = {
                "status": "registered-and-receipt-verified",
                "transaction_id": "2.1",
                "evidence_sha256": "ab" * 32,
            }
            transparency["service_url"] = "https://scitt.example"
            bundle_response = MagicMock()
            bundle_response.__enter__.return_value.read.return_value = b"receiver-published-bundle"
            with (
                patch("agent.sello_client.open_receiver", return_value=bundle_response) as transport,
                patch("agent.sello_client.verify_publication_bundle", return_value=transparency) as verify_log,
                patch("agent.transparency_index.record_transparency_entry", return_value={"record_id": "record-1"}),
            ):
                result = complete_receiver_call(
                    call,
                    {
                        RECEIPT_HEADER: receipt_header(envelope),
                        SCITT_TRANSACTION_HEADER: "2.1",
                        SCITT_BUNDLE_URL_HEADER: "/v1/sello/receipts/publication-1",
                    },
                    action_output,
                    200,
                    receiver_base_url="https://worker0-8080.example",
                )
        read_receiver.assert_called_once()
        bundle_request = transport.call_args.args[0]
        self.assertIs(transport.call_args.kwargs["identity"], call.client_identity)
        self.assertEqual(bundle_request.get_header("Authorization"), call.headers["Authorization"])
        self.assertEqual(bundle_request.full_url, "https://worker0-8080.example/v1/sello/receipts/publication-1")
        verify_log.assert_called_once_with(envelope, b"receiver-published-bundle")
        self.assertEqual(result["transaction_id"], "2.1")
        self.assertEqual(result["transparency_record_id"], "record-1")

    def test_missing_owner_configuration_fails_before_network(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agent.sello_client.open_receiver") as transport,
            self.assertRaises(RuntimeError),
        ):
            begin_receiver_call("jobs:read", "tee-inference", b"", receiver_base_url="https://worker0.example")
        transport.assert_not_called()

    def test_missing_call_cannot_skip_receipt_verification(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "mandatory authorization"):
            complete_receiver_call(None, {}, b"{}", 200)

    def test_tee_call_rejects_endpoint_not_paired_with_registry_key(self) -> None:
        issuer = SigningKey(bytes.fromhex("51" * 32))
        environment = {
            "SELLO_REQUIRED": "1",
            "SELLO_TOKEN_ISSUER_SIGNING_SEED": b64url_encode(bytes(issuer)),
            "SELLO_OWNER_HPKE_PRIVATE_KEY": b64url_encode(bytes.fromhex("52" * 32)),
            "SELLO_SERVICE_REGISTRY": json.dumps({"zk-inference": b64url_encode(bytes.fromhex("53" * 32))}),
            "SELLO_LOG_URLS": "https://scitt.example",
            "EXPECTED_RUNTIME_RPC_URL": "https://runtime-8545.example",
            "EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "11" * 20,
            "TEE_INFERENCE_PARTICIPANT_ADDRESS": "0x" + "12" * 20,
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch(
                "agent.blockchain_source.read_sello_receiver",
                return_value={
                    "authorized": True,
                    "public_ip": "https://registered.example",
                    "sello_receipt_key": bytes.fromhex("54" * 32),
                },
            ),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            begin_receiver_call(
                "run_and_verify_tee_inference",
                "tee-inference",
                b"input",
                receiver_base_url="https://other.example",
            )


if __name__ == "__main__":
    unittest.main()
