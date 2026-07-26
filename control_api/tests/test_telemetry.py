from __future__ import annotations

import csv
import io
import os
import time
import unittest
import zipfile
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct

from control_api import server


class SignedTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        server.reset_runtime_telemetry()
        self.account = Account.from_key("0x" + "11" * 32)
        self.inventory = (
            '[{"slot":0,"account_address":"'
            + self.account.address
            + '","private_key":"0x'
            + "11" * 32
            + '","rsa_private_key":"unused","rsa_public_key":"unused"}]'
        )

    def payload(self, nonce: str = "12345678-1234-1234-1234-123456789abc") -> dict[str, object]:
        return {
            "account": self.account.address.lower(),
            "attributes": {"global_model_round": 1, "accuracy_percent": 75.5},
            "device_id": "0",
            "event": "aggregator.global_model_evaluation",
            "nonce": nonce,
            "timestamp_unix_ms": int(time.time() * 1000),
            "version": 1,
        }

    def signature(self, payload: dict[str, object], key: str = "0x" + "11" * 32) -> str:
        canonical = server._canonical_telemetry_payload(payload)
        return Account.sign_message(encode_defunct(text=canonical), private_key=key).signature.hex()

    def test_signed_event_updates_evaluation_metrics(self) -> None:
        payload = self.payload()
        with patch.dict(os.environ, {"DYNAMIC_WORKER_INVENTORY": self.inventory}):
            server._record_telemetry(payload, self.signature(payload))

        records = server.telemetry_evaluation_records(server._telemetry_snapshot())

        self.assertEqual(records[0]["round"], "1")
        self.assertEqual(records[0]["accuracy_percent"], "75.5")

    def test_signed_event_accepts_chunked_worker_inventory(self) -> None:
        payload = self.payload()
        with patch.dict(
            os.environ,
            {
                "DYNAMIC_WORKER_INVENTORY": "",
                "DYNAMIC_WORKER_INVENTORY_000": self.inventory,
            },
            clear=False,
        ):
            server._record_telemetry(payload, self.signature(payload))

        records = server.telemetry_evaluation_records(server._telemetry_snapshot())
        self.assertEqual(records[0]["round"], "1")

    def test_replayed_nonce_is_rejected(self) -> None:
        payload = self.payload()
        with patch.dict(os.environ, {"DYNAMIC_WORKER_INVENTORY": self.inventory}):
            server._record_telemetry(payload, self.signature(payload))
            with self.assertRaisesRegex(ValueError, "already used"):
                server._record_telemetry(payload, self.signature(payload))

    def test_wrong_worker_key_is_rejected(self) -> None:
        payload = self.payload()
        with patch.dict(os.environ, {"DYNAMIC_WORKER_INVENTORY": self.inventory}):
            with self.assertRaisesRegex(ValueError, "does not match"):
                server._record_telemetry(payload, self.signature(payload, "0x" + "22" * 32))

    def test_wrong_device_id_is_rejected(self) -> None:
        payload = self.payload()
        payload["device_id"] = "1"
        with patch.dict(os.environ, {"DYNAMIC_WORKER_INVENTORY": self.inventory}):
            with self.assertRaisesRegex(ValueError, "device ID"):
                server._record_telemetry(payload, self.signature(payload))

    def test_worker_cost_metrics_include_derived_gwei(self) -> None:
        metrics: list[str] = []
        server.append_transaction_cost_metrics(
            metrics,
            [
                {
                    "scope": "worker",
                    "costEth": "0.000000123",
                    "costEur": "0.000369",
                    "account": self.account.address,
                    "deviceId": "0",
                }
            ],
        )
        rendered = "\n".join(metrics)
        self.assertIn("dfl_worker_cost_gwei_total 123.0", rendered)
        self.assertIn("dfl_worker_cost_gwei_by_worker", rendered)
        self.assertIn('worker="VM-0"', rendered)

    def test_observability_export_contains_csv_tables(self) -> None:
        transaction = {
            "scope": "worker",
            "transactionHash": "0x1234",
            "costEth": "0.000000123",
            "costEur": "0.000369",
            "account": self.account.address,
            "deviceId": "0",
        }
        with (
            patch.object(server, "_telemetry_snapshot", return_value=[]),
            patch.object(server, "read_evaluation_summary_records", return_value=[{"round": "1"}]),
            patch.object(server, "read_transaction_cost_records", return_value=[transaction]),
        ):
            filename, archive_bytes = server.build_observability_export()

        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            transaction_name = next(name for name in archive.namelist() if name.endswith("transaction_costs.csv"))
            worker_name = next(name for name in archive.namelist() if name.endswith("worker_costs_by_worker.csv"))
            transaction_rows = list(csv.DictReader(io.TextIOWrapper(archive.open(transaction_name))))
            worker_rows = list(csv.DictReader(io.TextIOWrapper(archive.open(worker_name))))
        self.assertEqual(transaction_rows[0]["costGwei"], "123")
        self.assertEqual(worker_rows[0]["worker"], "VM-0")
        self.assertEqual(worker_rows[0]["costGwei"], "123")


if __name__ == "__main__":
    unittest.main()
