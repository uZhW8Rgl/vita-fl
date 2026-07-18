from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import cbor2

from agent_receipts.receiver_log import ReceiverTransparencyPublisher


class ReceiverPublicationTests(unittest.TestCase):
    def test_receiver_publishes_before_exposing_retrievable_bundle(self) -> None:
        registration = {
            "status": "registered-and-receipt-verified",
            "service_url": "https://scitt.example",
            "development_tls": True,
            "content_type": "application/vnd.master-thesis.agent-tool-receipt+cbor",
            "transaction_id": "7.4",
            "evidence_sha256": "11" * 32,
            "signed_statement_sha256": "22" * 32,
            "transparent_statement_sha256": "33" * 32,
            "transparent_statement_bytes": 10,
            "transparent_statement_path": "",
            "receipts": [{"regtxid": "7.4"}],
            "_signed_statement": b"signed",
            "_transparent_statement": b"included",
        }
        with tempfile.TemporaryDirectory() as directory:
            publisher = ReceiverTransparencyPublisher("tee-inference", directory)
            with (
                patch.dict("os.environ", {"SCITT_URL": "https://scitt.example"}),
                patch("agent_receipts.receiver_log.register_verified_evidence", return_value=registration) as register,
            ):
                publication_id, public = publisher.publish(b"receiver-envelope", "https://scitt.example")
            stored = publisher.read(publication_id)

        register.assert_called_once()
        decoded = cbor2.loads(stored)
        self.assertEqual(decoded[1], b"signed")
        self.assertEqual(decoded[2], b"included")
        self.assertEqual(public["transaction_id"], "7.4")
        self.assertNotIn("_signed_statement", public)

    def test_receiver_rejects_token_log_substitution_before_submission(self) -> None:
        publisher = ReceiverTransparencyPublisher("zk-inference", "/tmp/unused-sello-test")
        with (
            patch.dict("os.environ", {"SCITT_URL": "https://trusted.example"}),
            patch("agent_receipts.receiver_log.register_verified_evidence") as register,
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            publisher.publish(b"receiver-envelope", "https://attacker.example")
        register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
