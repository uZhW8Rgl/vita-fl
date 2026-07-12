from __future__ import annotations

import hashlib
import unittest

from nacl.signing import SigningKey

from tee_inference.air.v1 import (
    AirClaims,
    AirPolicy,
    AirVerificationError,
    MODEL_HASH,
    emit_receipt,
    verify_receipt,
)

# Official cyntrisec/air-v1 valid/v1-tdx-with-nonce.json fixture (v1.0.1).
OFFICIAL_PUBLIC_KEY = bytes.fromhex(
    "197f6b23e16c8532c6abc838facd5ea789be0c76b2920334039bfa8b3d368d61"
)
OFFICIAL_TDX_RECEIPT = bytes.fromhex(
    "d28446a2012703183da0590211b1016d63796e7472697365632e636f6d061a67bdec8407501112131415161718191a1b1c1d1e1f200a48deadbeefcafebabe190109782168747470733a2f2f737065632e63796e7472697365632e636f6d2f6169722f76313a00010000686c6c616d612d37623a0001000165322e302e303a00010002582055555555555555555555555555555555555555555555555555555555555555553a00010003582066666666666666666666666666666666666666666666666666666666666666663a00010004582077777777777777777777777777777777777777777777777777777777777777773a00010005582088888888888888888888888888888888888888888888888888888888888888883a00010006a4647063723058301010101010101010101010101010101010101010101010101010101010101010101010101010101010101010101010106470637231583020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202020202064706372325830303030303030303030303030303030303030303030303030303030303030303030303030303030303030303030303030706d6561737572656d656e745f747970656d7464782d6d7274642d72746d723a000100076e706f6c6963792d323032362e30333a00010008013a000100091909c43a0001000a1920003a0001000b6a70726f64756374696f6e5840da9697770b22450d3a61234e84e330e3829e4c5a51bc6f897964a83de1c86cd2ae1049a050681ec37f33f7fc380a0463ca45bbb51b8beb75ea57f80b66442b03"
)


class AirV1Tests(unittest.TestCase):
    def fixture_claims(self) -> AirClaims:
        return AirClaims(
            issuer="tee-inference.test",
            issued_at=1_750_000_000,
            cti=bytes.fromhex("00112233445566778899aabbccddeeff"),
            nonce=bytes.fromhex("a5" * 32),
            model_id="master-thesis/chestmnist-dfl",
            model_version="round-1",
            model_hash=hashlib.sha256(b"manifest").digest(),
            request_hash=hashlib.sha256(b"request").digest(),
            response_hash=hashlib.sha256(b"response").digest(),
            attestation_doc_hash=hashlib.sha256(b"tdx-quote").digest(),
            enclave_measurements={
                "measurement_type": "tdx-mrtd-rtmr",
                "pcr0": bytes.fromhex("10" * 48),
                "pcr1": bytes.fromhex("20" * 48),
                "pcr2": bytes.fromhex("30" * 48),
                "pcr3": bytes.fromhex("40" * 48),
                "pcr4": bytes.fromhex("50" * 48),
            },
            policy_version="chestmnist-air-v1",
            sequence_number=1,
            execution_time_ms=12,
            memory_peak_mb=128,
            security_mode="production",
        )

    def test_verifies_official_tdx_golden_vector(self) -> None:
        claims = verify_receipt(
            OFFICIAL_TDX_RECEIPT,
            OFFICIAL_PUBLIC_KEY,
            AirPolicy(
                expected_nonce=bytes.fromhex("deadbeefcafebabe"),
                expected_model_hash=bytes.fromhex("55" * 32),
                expected_platform="tdx-mrtd-rtmr",
                allow_evaluation_mode=False,
            ),
            now=1_740_500_100,
        )
        self.assertEqual(claims[MODEL_HASH], bytes.fromhex("55" * 32))

    def test_emit_is_deterministic_and_round_trips(self) -> None:
        key = SigningKey(bytes.fromhex("2a" * 32))
        first = emit_receipt(self.fixture_claims(), key)
        second = emit_receipt(self.fixture_claims(), key)
        self.assertEqual(first, second)
        verified = verify_receipt(
            first,
            key.verify_key,
            AirPolicy(
                expected_nonce=bytes.fromhex("a5" * 32),
                expected_request_hash=hashlib.sha256(b"request").digest(),
                expected_response_hash=hashlib.sha256(b"response").digest(),
                expected_security_mode="production",
            ),
            now=1_750_000_000,
        )
        self.assertEqual(verified[MODEL_HASH], hashlib.sha256(b"manifest").digest())

    def test_wrong_key_is_rejected(self) -> None:
        receipt = emit_receipt(self.fixture_claims(), SigningKey(bytes.fromhex("2a" * 32)))
        with self.assertRaisesRegex(AirVerificationError, "signature") as raised:
            verify_receipt(receipt, SigningKey(bytes.fromhex("2b" * 32)).verify_key)
        self.assertEqual(raised.exception.code, "SIG_FAILED")

    def test_nonce_mismatch_is_rejected(self) -> None:
        key = SigningKey(bytes.fromhex("2a" * 32))
        receipt = emit_receipt(self.fixture_claims(), key)
        with self.assertRaises(AirVerificationError) as raised:
            verify_receipt(receipt, key.verify_key, AirPolicy(expected_nonce=bytes.fromhex("ff" * 32)))
        self.assertEqual(raised.exception.code, "NONCE_MISMATCH")

    def test_tampered_receipt_is_rejected(self) -> None:
        key = SigningKey(bytes.fromhex("2a" * 32))
        receipt = bytearray(emit_receipt(self.fixture_claims(), key))
        receipt[-1] ^= 1
        with self.assertRaises(AirVerificationError) as raised:
            verify_receipt(bytes(receipt), key.verify_key)
        self.assertEqual(raised.exception.code, "SIG_FAILED")


if __name__ == "__main__":
    unittest.main()

