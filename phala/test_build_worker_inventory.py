from __future__ import annotations

import unittest

from phala.build_worker_inventory import build_inventory


class BuildWorkerInventoryTests(unittest.TestCase):
    def test_existing_values_are_preserved(self) -> None:
        values = {}
        for slot in range(2):
            values.update(
                {
                    f"W{slot}_ACCOUNT_ADDRESS": "0x" + f"{slot + 1:040x}",
                    f"W{slot}_PRIVATE_KEY": "0x" + f"{slot + 1:064x}",
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
                    "W0_RSA_PRIVATE_KEY": "rsa-private",
                },
                1,
            )


if __name__ == "__main__":
    unittest.main()
