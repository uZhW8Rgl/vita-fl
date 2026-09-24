from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from nacl.signing import SigningKey

from agent_receipts.environment import owner_from_environment, receiver_from_environment
from agent_receipts.sello_v1 import b64url_encode


class MandatorySelloEnvironmentTests(unittest.TestCase):
    def test_receiver_cannot_start_without_issuer_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for service in ("tee-inference", "zk-inference"):
                with self.subTest(service=service), self.assertRaisesRegex(RuntimeError, "required"):
                    receiver_from_environment(service)

    def test_owner_cannot_start_without_each_required_component(self) -> None:
        complete = {
            "SELLO_TOKEN_ISSUER_SIGNING_SEED": b64url_encode(bytes.fromhex("11" * 32)),
            "SELLO_OWNER_HPKE_PRIVATE_KEY": b64url_encode(bytes.fromhex("22" * 32)),
            "SELLO_SERVICE_REGISTRY": "{}",
            "SELLO_LOG_URLS": "https://scitt.example",
        }
        for missing in complete:
            with (
                self.subTest(missing=missing),
                patch.dict(os.environ, {k: v for k, v in complete.items() if k != missing}, clear=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "required|configure"):
                    owner_from_environment()
        with patch.dict(os.environ, complete, clear=True):
            self.assertIsNotNone(owner_from_environment())

    def test_local_receiver_still_uses_scoped_key_derivation(self) -> None:
        issuer = SigningKey(bytes.fromhex("11" * 32))
        environment = {
            "LOCAL_TDX_MOCK": "1",
            "SELLO_SERVICE_KEY_PROVIDER": "env",
            "SELLO_SERVICE_SIGNING_SEED": b64url_encode(bytes.fromhex("22" * 32)),
            "SELLO_TOKEN_ISSUER_PUBLIC_KEY": b64url_encode(bytes(issuer.verify_key)),
            "ACCOUNT_ADDRESS": "0x" + "33" * 20,
            "REGISTRY_ADDRESS": "0x" + "44" * 20,
            "EXPECTED_CHAIN_ID": "31337",
        }
        with patch.dict(os.environ, environment, clear=True):
            receiver = receiver_from_environment("tee-inference")
        self.assertEqual(receiver.service_id, "tee-inference")
        self.assertNotEqual(bytes(receiver.signing_key), bytes.fromhex("22" * 32))
