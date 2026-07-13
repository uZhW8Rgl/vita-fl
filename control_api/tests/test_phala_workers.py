from __future__ import annotations

import json
import unittest

from control_api.phala_workers import (
    PhalaWorkerController,
    WorkerConfigurationError,
    load_worker_inventory,
)


def inventory_json(count: int = 3) -> str:
    return json.dumps(
        [
            {
                "slot": slot,
                "account_address": "0x" + f"{slot + 1:040x}",
                "private_key": "0x" + f"{slot + 1:064x}",
                "rsa_private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
                "rsa_public_key": "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----",
            }
            for slot in range(count)
        ]
    )


class FakeRunner:
    def __init__(self) -> None:
        self.workers: dict[str, dict[str, object]] = {}

    def apply(self, workers: dict[str, dict[str, object]]) -> dict[str, object]:
        self.workers = {
            key: {
                "app_id": f"app-{key}",
                "primary_cvm_id": f"cvm-{key}",
                "status": "running",
            }
            for key in workers
        }
        return self.workers

    def status(self) -> dict[str, object]:
        return self.workers


class FakeChallengeIssuer:
    def __init__(self) -> None:
        self.allowed: list[str] = []
        self.revoked: list[str] = []

    def allow(self, account_address: str) -> None:
        self.allowed.append(account_address)

    def revoke(self, account_address: str) -> None:
        self.revoked.append(account_address)


def controller(count: int = 3) -> tuple[PhalaWorkerController, FakeRunner, FakeChallengeIssuer]:
    runner = FakeRunner()
    issuer = FakeChallengeIssuer()
    instance = PhalaWorkerController(load_worker_inventory(inventory_json(count)), runner, issuer)
    return instance, runner, issuer


class WorkerInventoryTests(unittest.TestCase):
    def test_inventory_is_ordered_and_never_exposes_keys(self) -> None:
        instance, _runner, issuer = controller()

        result = instance.scale(2)

        self.assertEqual(result["desired_worker_count"], 2)
        self.assertEqual([worker["slot"] for worker in result["workers"]], [0, 1])
        serialized = json.dumps(result)
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("BEGIN PRIVATE KEY", serialized)
        self.assertEqual(len(issuer.allowed), 2)

    def test_scale_down_removes_highest_slots_first(self) -> None:
        instance, _runner, issuer = controller()
        instance.scale(3)

        result = instance.scale(1)

        self.assertEqual([worker["worker"] for worker in result["workers"]], ["worker0"])
        self.assertEqual(len(issuer.allowed), 3)

    def test_non_contiguous_inventory_is_rejected(self) -> None:
        payload = json.loads(inventory_json(2))
        payload[1]["slot"] = 3
        with self.assertRaisesRegex(WorkerConfigurationError, "contiguous"):
            load_worker_inventory(json.dumps(payload))

    def test_out_of_range_scale_is_rejected_without_apply(self) -> None:
        instance, runner, issuer = controller(2)
        with self.assertRaisesRegex(WorkerConfigurationError, "between 0 and 2"):
            instance.scale(3)
        self.assertEqual(runner.workers, {})
        self.assertEqual(issuer.allowed, [])


if __name__ == "__main__":
    unittest.main()
