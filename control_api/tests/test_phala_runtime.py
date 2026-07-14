from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from control_api import server


class PhalaRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_user_rounds_exclude_bootstrap_round(self) -> None:
        config = {"rounds": 3, "epoch": 2, "worker_count": 4, "client_limit": 3}

        translated = server.phala_worker_training_config(config)

        self.assertEqual(translated["rounds"], 4)
        self.assertEqual(translated["epoch"], 2)
        self.assertEqual(config["rounds"], 3)

    async def test_phala_contract_initialization_restarts_anvil_prometheus_and_contracts(self) -> None:
        controller = Mock()
        controller.scale.return_value = {"deployed_worker_count": 0, "workers": []}
        contract_state = {"status": "exited", "exit_code": 0}
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
                "wait_for_docker_container_exit_success",
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
