from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from nacl.signing import SigningKey

from agent.sello_client import begin_receiver_call, complete_receiver_call
from agent_receipts.environment import RECEIPT_HEADER, receipt_header
from agent_receipts.receiver_log import SCITT_TRANSACTION_HEADER
from agent_receipts.sello_v1 import SelloReceiver, b64url_encode


class SelloClientTests(unittest.TestCase):
    def test_receiver_receipt_is_verified_before_scitt_indexing(self) -> None:
        issuer = SigningKey(bytes.fromhex("41" * 32))
        service = SigningKey(bytes.fromhex("42" * 32))
        environment = {
            "SELLO_REQUIRED": "1",
            "SELLO_TOKEN_ISSUER_SIGNING_SEED": b64url_encode(bytes(issuer)),
            "SELLO_OWNER_HPKE_PRIVATE_KEY": b64url_encode(bytes.fromhex("43" * 32)),
            "SELLO_SERVICE_REGISTRY": json.dumps({"tee-inference": b64url_encode(bytes(service.verify_key))}),
            "SELLO_LOG_URLS": "https://scitt.example",
        }
        action_input = b'{"index":0}'
        action_output = b'{"ok":true}'
        with patch.dict(os.environ, environment, clear=False):
            call = begin_receiver_call("generate_random_tee_chestmnist_image", "tee-inference", action_input)
            self.assertIsNotNone(call)
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
            with (
                patch("agent.sello_client.verify_publication_bundle", return_value=transparency) as verify_log,
                patch("agent.transparency_index.record_transparency_entry", return_value={"record_id": "record-1"}),
            ):
                result = complete_receiver_call(
                    call,
                    {
                        RECEIPT_HEADER: receipt_header(envelope),
                        SCITT_TRANSACTION_HEADER: "2.1",
                    },
                    action_output,
                    200,
                    publication_bundle=b"receiver-published-bundle",
                )
        verify_log.assert_called_once_with(envelope, b"receiver-published-bundle")
        self.assertEqual(result["transaction_id"], "2.1")
        self.assertEqual(result["transparency_record_id"], "record-1")


if __name__ == "__main__":
    unittest.main()
