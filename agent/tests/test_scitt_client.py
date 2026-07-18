from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from agent.scitt_client import SCITT_ZK_CONTENT_TYPE, ScittRegistrationError, register_verified_evidence


class ScittClientTests(unittest.TestCase):
    def test_verified_transparent_statement_is_persisted(self) -> None:
        evidence = b"deterministic evidence"
        signed = b"signed statement"
        transparent = b"transparent statement with receipt"
        submission = SimpleNamespace(tx="3.14", response_bytes=transparent)
        details = [{"iss": "log", "iat": 1, "sigtxid": "3.15", "regtxid": "3.14"}]

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "statement.cose"
            with (
                patch("agent_receipts.scitt._load_or_create_signer", return_value=object()),
                patch("agent_receipts.scitt._sign_evidence", return_value=signed),
                patch("agent_receipts.scitt._submit_and_verify", return_value=(submission, details)),
            ):
                result = register_verified_evidence(
                    evidence,
                    url="https://scitt.example",
                    development=False,
                    signer_dir=str(Path(directory) / "identity"),
                    transparent_statement_path=str(output),
                )

            self.assertEqual(output.read_bytes(), transparent)
            self.assertEqual(result["transaction_id"], "3.14")
            self.assertEqual(result["evidence_sha256"], hashlib.sha256(evidence).hexdigest())
            self.assertEqual(result["receipts"], details)
            self.assertFalse(result["development_tls"])

    def test_empty_evidence_is_rejected_before_submission(self) -> None:
        with self.assertRaisesRegex(ScittRegistrationError, "empty"):
            register_verified_evidence(b"")

    def test_plain_http_service_is_rejected(self) -> None:
        with self.assertRaisesRegex(ScittRegistrationError, "HTTPS"):
            register_verified_evidence(b"evidence", url="http://scitt.example")

    def test_zk_proof_content_type_is_bound_into_statement(self) -> None:
        submission = SimpleNamespace(tx="4.1", response_bytes=b"transparent")
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("agent_receipts.scitt._load_or_create_signer", return_value=object()),
                patch("agent_receipts.scitt._sign_evidence", return_value=b"signed") as sign,
                patch(
                    "agent_receipts.scitt._submit_and_verify",
                    return_value=(submission, [{"regtxid": "4.1"}]),
                ),
            ):
                result = register_verified_evidence(
                    b"zk proof bundle",
                    content_type=SCITT_ZK_CONTENT_TYPE,
                    transparent_statement_path=str(Path(directory) / "zk.cose"),
                )
        sign.assert_called_once_with(b"zk proof bundle", ANY, SCITT_ZK_CONTENT_TYPE)
        self.assertEqual(result["content_type"], SCITT_ZK_CONTENT_TYPE)


if __name__ == "__main__":
    unittest.main()
