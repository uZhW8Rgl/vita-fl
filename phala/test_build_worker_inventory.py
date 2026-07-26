from __future__ import annotations

import json
from pathlib import Path
import unittest

from phala.build_worker_inventory import (
    DEFAULT_CHUNK_BYTES,
    INVENTORY_CHUNK_PREFIX,
    build_inventory,
    chunk_inventory,
    read_env_files,
)


ROOT = Path(__file__).resolve().parents[1]


class BuildWorkerInventoryTests(unittest.TestCase):
    def test_existing_values_are_preserved(self) -> None:
        values = {}
        for slot in range(2):
            values.update(
                {
                    f"W{slot}_ACCOUNT_ADDRESS": "0x" + f"{slot + 1:040x}",
                    f"W{slot}_PRIVATE_KEY": "0x" + f"{slot + 1:064x}",
                    f"W{slot}_DEVICE_ID": str(slot),
                    f"W{slot}_RSA_PRIVATE_KEY": f"private-{slot}",
                    f"W{slot}_RSA_PUBLIC_KEY": f"public-{slot}",
                }
            )

        inventory = build_inventory(values, 2)

        self.assertEqual(inventory[1]["rsa_private_key"], "private-1")
        self.assertEqual(inventory[1]["private_key"], "0x" + f"{2:064x}")

    def test_incomplete_fixed_slot_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "W0_RSA_PUBLIC_KEY"):
            build_inventory(
                {
                    "W0_ACCOUNT_ADDRESS": "address",
                    "W0_PRIVATE_KEY": "key",
                    "W0_DEVICE_ID": "0",
                    "W0_RSA_PRIVATE_KEY": "rsa-private",
                },
                1,
            )

    def test_inline_rsa_newlines_are_decoded(self) -> None:
        values = {
            "W0_ACCOUNT_ADDRESS": "0x" + "01" * 20,
            "W0_PRIVATE_KEY": "0x" + "02" * 32,
            "W0_DEVICE_ID": "0",
            "W0_RSA_PRIVATE_KEY": "explicit-private\\nvalue",
            "W0_RSA_PUBLIC_KEY": "explicit-public\\nvalue",
        }

        inventory = build_inventory(values, 1)

        self.assertEqual(inventory[0]["rsa_private_key"], "explicit-private\nvalue")
        self.assertEqual(inventory[0]["rsa_public_key"], "explicit-public\nvalue")

    def test_device_id_must_match_slot(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected 0"):
            build_inventory(
                {
                    "W0_ACCOUNT_ADDRESS": "address",
                    "W0_PRIVATE_KEY": "key",
                    "W0_DEVICE_ID": "17",
                    "W0_RSA_PRIVATE_KEY": "rsa-private",
                    "W0_RSA_PUBLIC_KEY": "rsa-public",
                },
                1,
            )

    def test_phala_example_contains_all_500_worker_identities(self) -> None:
        inventory = build_inventory(
            read_env_files([ROOT / ".env.phala.anvil.example"]),
            500,
        )

        self.assertEqual(len(inventory), 500)
        self.assertEqual(inventory[-1]["slot"], 499)
        self.assertTrue(str(inventory[-1]["rsa_private_key"]).startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertTrue(str(inventory[-1]["rsa_public_key"]).startswith("-----BEGIN PUBLIC KEY-----"))

    def test_full_inventory_is_split_below_single_environment_entry_limit(self) -> None:
        inventory = build_inventory(
            read_env_files([ROOT / ".env.phala.anvil.example"]),
            500,
        )

        chunks = chunk_inventory(inventory)
        restored = [
            record
            for name in sorted(chunks)
            for record in json.loads(chunks[name])
        ]

        self.assertEqual(restored, inventory)
        self.assertEqual(next(iter(chunks)), f"{INVENTORY_CHUNK_PREFIX}000")
        self.assertTrue(all(len(value.encode("utf-8")) <= DEFAULT_CHUNK_BYTES for value in chunks.values()))


if __name__ == "__main__":
    unittest.main()
