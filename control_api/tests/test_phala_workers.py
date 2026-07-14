from __future__ import annotations

import json
import unittest

from control_api.phala_workers import (
    PhalaWorkerController,
    WorkerConfigurationError,
    load_worker_inventory,
    redact_terraform_output,
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
        self.training_config: dict[str, int] = {}

    def configure(self, training_config: dict[str, int]) -> None:
        self.training_config = dict(training_config)

    def current_training_config(self) -> dict[str, int]:
        return dict(self.training_config)

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
    def test_terraform_diagnostics_are_redacted(self) -> None:
        private_key = "0x" + "ab" * 32
        diagnostic = (
            f"key={private_key}\n"
            "token=phak_example-token\n"
            "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
        )

        redacted = redact_terraform_output(diagnostic)

        self.assertNotIn(private_key, redacted)
        self.assertNotIn("phak_example-token", redacted)
        self.assertNotIn("secret", redacted)

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

    def test_training_configuration_is_forwarded_to_terraform(self) -> None:
        instance, runner, _issuer = controller()

        instance.scale(2, {"rounds": 7, "epoch": 3, "client_limit": 1})

        self.assertEqual(runner.training_config, {"rounds": 7, "epoch": 3, "client_limit": 1})

    def test_attested_worker_configuration_cannot_be_mutated(self) -> None:
        instance, runner, _issuer = controller()
        initial = {"rounds": 2, "epoch": 1, "client_limit": 1}
        instance.scale(2, initial)

        with self.assertRaisesRegex(WorkerConfigurationError, "cannot mutate an attested worker compose"):
            instance.scale(2, {"rounds": 3, "epoch": 1, "client_limit": 1})

        self.assertEqual(runner.training_config, initial)

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
