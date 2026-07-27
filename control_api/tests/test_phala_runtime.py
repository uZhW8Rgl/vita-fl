from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control_api import server


class PhalaRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_training_config_exposes_500_slots_with_safe_initial_selection(self) -> None:
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.dict(
                server.os.environ,
                {
                    "MAX_DYNAMIC_WORKERS": "500",
                    "WORKER_COUNT": "3",
                    "CLIENT_LIMIT": "2",
                },
                clear=False,
            ),
        ):
            config = server.read_training_config(
                Path("/nonexistent/training.env"),
                Path("/nonexistent/compose.yml"),
            )

        self.assertEqual(config["worker_count"], 3)
        self.assertEqual(config["max_worker_count"], 500)
        self.assertEqual(config["available_workers"][0], "worker0")
        self.assertEqual(config["available_workers"][-1], "worker499")

    def test_user_rounds_exclude_bootstrap_round(self) -> None:
        config = {"rounds": 3, "epoch": 2, "worker_count": 4, "client_limit": 3}

        translated = server.phala_worker_training_config(config)

        self.assertEqual(translated["rounds"], 4)
        self.assertEqual(translated["epoch"], 2)
        self.assertEqual(config["rounds"], 3)

    def test_bootstrap_recipient_declaration_uses_admission_generation(self) -> None:
        first = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        second = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        admission = {
            "status": "admission-ready",
            "chain_id": "31337",
            "registry_address": "0x" + "11" * 20,
            "expected_worker_image_digest": "0x" + "22" * 32,
        }
        with (
            patch.object(server, "read_runtime_mfs_marker", return_value=admission),
            patch.object(
                server,
                "write_runtime_mfs_marker",
                side_effect=lambda _path, value: value,
            ) as write_marker,
        ):
            declaration = server.publish_bootstrap_recipient_declaration(
                [first, second]
            )

        self.assertEqual(declaration["status"], "declared")
        self.assertEqual(declaration["chain_id"], "31337")
        self.assertEqual(
            declaration["recipients"],
            [first.lower(), second.lower()],
        )
        self.assertEqual(declaration["worker_count"], 2)
        self.assertEqual(
            write_marker.call_args.args[0],
            "/runtime/bootstrap-recipients.json",
        )

    def test_bootstrap_recipient_declaration_rejects_duplicates(self) -> None:
        address = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        with self.assertRaisesRegex(ValueError, "unique"):
            server.publish_bootstrap_recipient_declaration(
                [address, address.lower()]
            )

    async def test_phala_contract_initialization_restarts_anvil_prometheus_and_contracts(self) -> None:
        controller = Mock()
        controller.scale.return_value = {"deployed_worker_count": 0, "workers": []}
        contract_state = {
            "status": "running",
            "phase": "admission-ready",
            "admission_marker": {"status": "admission-ready"},
        }
        operation_order: list[str] = []

        async def run_subprocess(command, **_kwargs):
            operation_order.append(command[1])
            return {"returncode": 0}

        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(server, "reset_runtime_telemetry") as reset_telemetry,
            patch.object(server, "clear_evaluation_artifacts", new=AsyncMock(return_value=[])),
            patch.object(
                server,
                "phala_container_id",
                new=AsyncMock(return_value="contract-container-id"),
            ),
            patch.object(server, "resolve_docker_bin", return_value="docker"),
            patch.object(server, "run_subprocess", side_effect=run_subprocess),
            patch.object(
                server,
                "restart_phala_container",
                new=AsyncMock(side_effect=[{"service": "anvil"}, {"service": "prometheus"}]),
            ) as restart_container,
            patch.object(
                server,
                "wait_for_runtime_admission_ready",
                new=AsyncMock(return_value=contract_state),
            ),
            patch.object(server, "phala_runtime_status", new=AsyncMock(return_value={"contract_initialized": True})),
        ):
            result = await server.initialize_contract_stack()

        controller.scale.assert_called_once_with(0)
        reset_telemetry.assert_called_once_with()
        self.assertEqual(operation_order, ["stop", "start"])
        self.assertEqual(
            [call.args for call in restart_container.await_args_list],
            [("anvil",), ("prometheus", "http://prometheus:9090/-/ready")],
        )
        self.assertEqual(result["contract_state"], contract_state)
        self.assertEqual(result["phala_workers"]["deployed_worker_count"], 0)

    async def test_runtime_admission_wait_returns_only_for_valid_marker(self) -> None:
        state = {"status": "running", "running": True}
        with (
            patch.object(
                server,
                "inspect_docker_container",
                new=AsyncMock(return_value=state),
            ),
            patch.object(
                server,
                "read_runtime_mfs_marker",
                return_value={"status": "admission-ready", "chain_id": "31337"},
            ),
        ):
            result = await server.wait_for_runtime_admission_ready("container", 2)

        self.assertEqual(result["phase"], "admission-ready")
        self.assertEqual(result["admission_marker"]["chain_id"], "31337")

    async def test_local_admission_wait_uses_compose_service_state(self) -> None:
        state = {
            "service": "smart-contracts",
            "status": "running",
            "running": True,
        }
        with (
            patch.object(
                server,
                "inspect_service",
                new=AsyncMock(return_value=state),
            ),
            patch.object(
                server,
                "read_runtime_mfs_marker",
                return_value={"status": "admission-ready"},
            ),
        ):
            result = await server.wait_for_local_runtime_admission_ready(2)

        self.assertEqual(result["phase"], "admission-ready")
        self.assertEqual(result["service"], "smart-contracts")

    async def test_phala_grafana_reset_clears_sources_and_restarts_prometheus(self) -> None:
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "environment_flag", return_value=True),
            patch.object(server, "reset_runtime_telemetry") as reset_telemetry,
            patch.object(server, "clear_evaluation_artifacts", new=AsyncMock(return_value=[])) as clear_artifacts,
            patch.object(
                server,
                "restart_phala_container",
                new=AsyncMock(return_value={"service": "prometheus"}),
            ) as restart_container,
            patch.object(server, "phala_runtime_status", new=AsyncMock(return_value={})) as runtime_status,
        ):
            result = await server.reset_observability_values()

        reset_telemetry.assert_called_once_with()
        clear_artifacts.assert_awaited_once_with()
        restart_container.assert_awaited_once_with("prometheus", "http://prometheus:9090/-/ready")
        runtime_status.assert_awaited_once_with()
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
