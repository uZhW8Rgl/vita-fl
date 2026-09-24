from __future__ import annotations

import unittest

from nacl.signing import SigningKey

from agent_receipts.sello_v1 import (
    ReceiptVerificationError,
    SelloOwner,
    SelloReceiver,
    b64url_encode,
    verify_authorization_token,
)


class SelloV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.receiver_key = SigningKey(bytes.fromhex("11" * 32))
        self.owner = SelloOwner(
            SigningKey(bytes.fromhex("22" * 32)),
            bytes.fromhex("33" * 32),
            {"tee-inference": self.receiver_key.verify_key},
            subject="master-thesis-agent",
            log_urls=["https://scitt.example"],
        )
        self.receiver = SelloReceiver(
            "tee-inference",
            self.receiver_key,
            self.owner.token_issuer_public_key,
        )
        self.fingerprint = b64url_encode(bytes.fromhex("44" * 32))
        self.token = self.owner.token(
            audience="https://worker.example",
            cert_thumbprint=self.fingerprint,
            scopes=["fetch_latest_verified_tee_model_bundle", "receipts:read"],
            now=1_800_000_000,
        )

    def test_http_authorization_requires_audience_scope_subject_and_certificate_match(self) -> None:
        checks = {
            "expected_audience": "https://worker.example",
            "required_scope": "fetch_latest_verified_tee_model_bundle",
            "expected_subject": "master-thesis-agent",
            "cert_thumbprint": self.fingerprint,
        }
        claims = verify_authorization_token(self.token, self.owner.token_issuer_public_key, now=1_800_000_001, **checks)
        self.assertEqual(claims["exp"] - claims["iat"], 300)
        for key, replacement in {
            "expected_audience": "https://another.example",
            "required_scope": "run_and_verify_tee_inference",
            "expected_subject": "another-agent",
            "cert_thumbprint": b64url_encode(bytes.fromhex("55" * 32)),
        }.items():
            with self.subTest(claim=key), self.assertRaises(ReceiptVerificationError):
                verify_authorization_token(
                    self.token, self.owner.token_issuer_public_key, now=1_800_000_001, **{**checks, key: replacement}
                )

    def test_tokens_minted_in_same_second_have_distinct_identifiers(self) -> None:
        second = self.owner.token(
            audience="https://worker.example",
            cert_thumbprint=self.fingerprint,
            scopes=["fetch_latest_verified_tee_model_bundle", "receipts:read"],
            now=1_800_000_000,
        )
        first_claims = verify_authorization_token(self.token, self.owner.token_issuer_public_key, now=1_800_000_000)
        second_claims = verify_authorization_token(second, self.owner.token_issuer_public_key, now=1_800_000_000)
        self.assertNotEqual(first_claims["jti"], second_claims["jti"])

    def emit(self) -> bytes:
        return self.receiver.issue(
            self.token,
            action_type="fetch_latest_verified_tee_model_bundle",
            action_input=b"{}",
            action_output=b'{"ok":true}',
            result_status="success",
            now=1_800_000_001,
        )

    def test_receiver_receipt_round_trip(self) -> None:
        verified = self.owner.verify(
            self.emit(),
            self.token,
            expected_service="tee-inference",
            expected_action="fetch_latest_verified_tee_model_bundle",
            action_input=b"{}",
            action_output=b'{"ok":true}',
        )
        self.assertEqual(verified.body["result-status"], "success")
        self.assertEqual(verified.log_url, "https://scitt.example")

    def test_tampered_output_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReceiptVerificationError, "output hash"):
            self.owner.verify(
                self.emit(),
                self.token,
                expected_service="tee-inference",
                expected_action="fetch_latest_verified_tee_model_bundle",
                action_input=b"{}",
                action_output=b'{"ok":false}',
            )

    def test_wrong_receiver_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReceiptVerificationError, "trusted registry"):
            self.owner.verify(
                self.emit(),
                self.token,
                expected_service="zk-inference",
                expected_action="fetch_latest_verified_tee_model_bundle",
                action_input=b"{}",
                action_output=b'{"ok":true}',
            )

    def test_explicit_admission_bound_key_overrides_static_service_registry(self) -> None:
        owner = SelloOwner(
            SigningKey(bytes.fromhex("22" * 32)),
            bytes.fromhex("33" * 32),
            {"tee-inference": SigningKey(bytes.fromhex("44" * 32)).verify_key},
            subject="master-thesis-agent",
            log_urls=["https://scitt.example"],
        )
        verified = owner.verify(
            self.emit(),
            self.token,
            expected_service="tee-inference",
            expected_action="fetch_latest_verified_tee_model_bundle",
            action_input=b"{}",
            action_output=b'{"ok":true}',
            trusted_service_key=bytes(self.receiver_key.verify_key),
        )
        self.assertEqual(verified.kid, self.receiver.kid)

    def test_all_six_public_tool_actions_round_trip(self) -> None:
        actions = (
            "fetch_latest_verified_tee_model_bundle",
            "generate_random_tee_chestmnist_image",
            "run_and_verify_tee_inference",
            "fetch_latest_verified_zk_model_bundle",
            "generate_random_zk_chestmnist_image",
            "generate_and_verify_zk_inference_proof",
        )
        for action in actions:
            with self.subTest(action=action):
                envelope = self.receiver.issue(
                    self.token,
                    action_type=action,
                    action_input=b"input",
                    action_output=b"output",
                    result_status="success",
                    now=1_800_000_002,
                )
                verified = self.owner.verify(
                    envelope,
                    self.token,
                    expected_service="tee-inference",
                    expected_action=action,
                    action_input=b"input",
                    action_output=b"output",
                )
                self.assertEqual(verified.body["action-type"], action)

    def test_receiver_error_result_is_signed(self) -> None:
        envelope = self.receiver.issue(
            self.token,
            action_type="fetch_latest_verified_tee_model_bundle",
            action_input=b"{}",
            action_output=b'{"error":"not ready"}',
            result_status="error",
            now=1_800_000_003,
        )
        verified = self.owner.verify(
            envelope,
            self.token,
            expected_service="tee-inference",
            expected_action="fetch_latest_verified_tee_model_bundle",
            action_input=b"{}",
            action_output=b'{"error":"not ready"}',
        )
        self.assertEqual(verified.body["result-status"], "error")


if __name__ == "__main__":
    unittest.main()
