from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

import cbor2

from agent.mcp_server import generate_and_verify_zk_inference_proof


class ZkTransparencyTests(unittest.TestCase):
    def test_verified_proof_bundle_is_registered_with_scitt(self) -> None:
        job_id = "ab" * 16
        model_id = "12" * 32
        bundle = cbor2.dumps(
            {
                "schema": "master-thesis.zk-inference-proof.v1",
                "job_id": job_id,
                "model_id": model_id,
                "proof_verified": True,
            },
            canonical=True,
        )
        proof_result = {
            "ok": True,
            "job_id": job_id,
            "model_id": model_id,
            "source_index": 2,
            "proof_verified": True,
            "artifact_sha256": {"proof.json": "34" * 32},
            "transparency_bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "tool_receipt": {"receiver_kid": bytes.fromhex("ab" * 32)},
        }
        transparency = {
            "status": "registered-and-receipt-verified",
            "transaction_id": "5.1",
            "content_type": "application/vnd.master-thesis.zk-inference-proof+cbor",
            "evidence_sha256": hashlib.sha256(bundle).hexdigest(),
            "signed_statement_sha256": "56" * 32,
            "transparent_statement_sha256": "78" * 32,
            "transparent_statement_bytes": 321,
            "receipts": [{"regtxid": "5.1", "claim_digest": bytes.fromhex("cd" * 32)}],
            "_signed_statement": b"signed statement",
            "_transparent_statement": b"transparent statement",
        }
        with (
            patch("agent.mcp_server._remote_call", return_value=proof_result),
            patch("agent.mcp_server._remote_get_bytes", return_value=bundle),
            patch("agent.scitt_client.register_verified_evidence", return_value=transparency) as register,
            patch(
                "agent.transparency_index.record_transparency_entry",
                return_value={"record_id": "90" * 32},
            ),
        ):
            result = json.loads(generate_and_verify_zk_inference_proof(job_id))

        register.assert_called_once()
        self.assertEqual(register.call_args.args[0], bundle)
        self.assertEqual(result["stage"], "proof-verified-and-transparency-logged")
        self.assertTrue(result["proof_verified"])
        self.assertEqual(result["transparency_log"]["transaction_id"], "5.1")
        self.assertEqual(result["tool_receipt"]["receiver_kid"], "ab" * 32)
        self.assertEqual(result["transparency_log"]["receipts"][0]["claim_digest"], "cd" * 32)
        self.assertNotIn("_signed_statement", result["transparency_log"])
        self.assertNotIn("_transparent_statement", result["transparency_log"])


if __name__ == "__main__":
    unittest.main()
