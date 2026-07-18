from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.transparency_index import read_transparency_entries, record_transparency_entry


class TransparencyIndexTests(unittest.TestCase):
    def test_verified_tee_and_zk_entries_are_visible_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            common = {
                "status": "registered-and-receipt-verified",
                "transaction_id": "2.1",
                "content_type": "application/vnd.master-thesis.tee-inference-evidence+cbor",
                "evidence_sha256": "11" * 32,
                "signed_statement_sha256": "22" * 32,
                "transparent_statement_sha256": "33" * 32,
                "transparent_statement_bytes": 123,
                "receipts": [{"regtxid": "2.1", "sigtxid": "2.2"}],
            }
            tee = record_transparency_entry(
                evidence_type="tee-inference-receipt",
                job_id="aa" * 16,
                model_id="44" * 32,
                transparency=common,
                verification={"bundle_sha256": "55" * 32},
                index_path=path,
            )
            zk_transparency = {
                **common,
                "transaction_id": "3.1",
                "content_type": "application/vnd.master-thesis.zk-inference-proof+cbor",
                "evidence_sha256": "66" * 32,
            }
            zk = record_transparency_entry(
                evidence_type="zk-inference-proof",
                job_id="bb" * 16,
                model_id="77" * 32,
                transparency=zk_transparency,
                verification={"proof_verified": True},
                index_path=path,
            )
            entries = read_transparency_entries(index_path=path)

        self.assertEqual([entry["record_id"] for entry in entries], [zk["record_id"], tee["record_id"]])
        self.assertEqual(entries[0]["receipt_transactions"][0]["sigtxid"], "2.2")
        self.assertTrue(entries[0]["verification"]["proof_verified"])


if __name__ == "__main__":
    unittest.main()
