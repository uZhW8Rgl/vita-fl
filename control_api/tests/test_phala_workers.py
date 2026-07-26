from __future__ import annotations

import json
import unittest

from control_api.phala_workers import (
    PhalaWorkerController,
    WorkerConfigurationError,
    WorkerDeploymentConfig,
    dynamic_worker_inventory_json_from_environment,
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


def controller(count: int = 3) -> tuple[PhalaWorkerController, FakeRunner]:
    runner = FakeRunner()
    instance = PhalaWorkerController(load_worker_inventory(inventory_json(count)), runner)
    return instance, runner


class WorkerInventoryTests(unittest.TestCase):
    def test_deployment_tfvars_include_the_measured_contract_trust_root(self) -> None:
        config = WorkerDeploymentConfig(
            phala_cloud_api_key="phak_test",
            worker_image="ghcr.io/example/worker@sha256:" + "11" * 32,
            rpc_url="https://rpc.example",
            kubo_api_url="https://kubo.example",
            kubo_gateway_url="https://gateway.example",
            telemetry_url="https://telemetry.example",
            expected_device_registry_address="0x" + "11" * 20,
            expected_aggregator_address="0x" + "22" * 20,
            expected_gm_storage_address="0x" + "33" * 20,
            expected_medical_signer_registry_address="0x" + "44" * 20,
            expected_chain_id=31337,
        )

        values = config.terraform_values()

        self.assertEqual(values["expected_device_registry_address"], "0x" + "11" * 20)
        self.assertEqual(values["expected_aggregator_address"], "0x" + "22" * 20)
        self.assertEqual(values["expected_gm_storage_address"], "0x" + "33" * 20)
        self.assertEqual(
            values["expected_medical_signer_registry_address"],
            "0x" + "44" * 20,
        )
        self.assertEqual(values["expected_chain_id"], 31337)

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
        instance, _runner = controller()

        result = instance.scale(2)

        self.assertEqual(result["desired_worker_count"], 2)
        self.assertEqual([worker["slot"] for worker in result["workers"]], [0, 1])
        serialized = json.dumps(result)
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("BEGIN PRIVATE KEY", serialized)

    def test_scale_down_removes_highest_slots_first(self) -> None:
        instance, _runner = controller()
        instance.scale(3)

        result = instance.scale(1)

        self.assertEqual([worker["worker"] for worker in result["workers"]], ["worker0"])

    def test_training_configuration_is_forwarded_to_terraform(self) -> None:
        instance, runner = controller()

        instance.scale(2, {"rounds": 7, "epoch": 3, "client_limit": 1})

        self.assertEqual(runner.training_config, {"rounds": 7, "epoch": 3, "client_limit": 1})

    def test_attested_worker_configuration_cannot_be_mutated(self) -> None:
        instance, runner = controller()
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

    def test_inventory_accepts_all_500_fixed_slots(self) -> None:
        identities = load_worker_inventory(inventory_json(500))

        self.assertEqual(len(identities), 500)
        self.assertEqual(identities[-1].slot, 499)

    def test_inventory_rejects_more_than_500_slots(self) -> None:
        with self.assertRaisesRegex(WorkerConfigurationError, "maximum of 500"):
            load_worker_inventory(inventory_json(501))

    def test_chunked_inventory_is_reassembled_in_numeric_order(self) -> None:
        records = json.loads(inventory_json(3))
        environment = {
            "DYNAMIC_WORKER_INVENTORY_001": json.dumps(records[2:]),
            "DYNAMIC_WORKER_INVENTORY_000": json.dumps(records[:2]),
        }

        restored = json.loads(dynamic_worker_inventory_json_from_environment(environment))

        self.assertEqual([record["slot"] for record in restored], [0, 1, 2])

    def test_inventory_reassembly_fails_closed_for_ambiguous_or_missing_chunks(self) -> None:
        with self.assertRaisesRegex(WorkerConfigurationError, "either"):
            dynamic_worker_inventory_json_from_environment(
                {
                    "DYNAMIC_WORKER_INVENTORY": inventory_json(1),
                    "DYNAMIC_WORKER_INVENTORY_000": inventory_json(1),
                }
            )
        with self.assertRaisesRegex(WorkerConfigurationError, "contiguous"):
            dynamic_worker_inventory_json_from_environment(
                {"DYNAMIC_WORKER_INVENTORY_001": inventory_json(1)}
            )
        with self.assertRaisesRegex(WorkerConfigurationError, "must contain a JSON array"):
            dynamic_worker_inventory_json_from_environment(
                {"DYNAMIC_WORKER_INVENTORY_000": "{}"}
            )

    def test_out_of_range_scale_is_rejected_without_apply(self) -> None:
        instance, runner = controller(2)
        with self.assertRaisesRegex(WorkerConfigurationError, "between 0 and 2"):
            instance.scale(3)
        self.assertEqual(runner.workers, {})


if __name__ == "__main__":
    unittest.main()
