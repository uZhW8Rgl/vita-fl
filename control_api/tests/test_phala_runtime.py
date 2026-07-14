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

    async def test_phala_contract_initialization_resets_workers_anvil_and_contracts(self) -> None:
        controller = Mock()
        controller.scale.return_value = {"deployed_worker_count": 0, "workers": []}
        contract_state = {"status": "exited", "exit_code": 0}

        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(server, "reset_runtime_telemetry") as reset_telemetry,
            patch.object(server, "clear_evaluation_artifacts", new=AsyncMock(return_value=[])),
            patch.object(
                server,
                "_post_json",
                return_value={"jsonrpc": "2.0", "id": 1, "result": True},
            ) as post,
            patch.object(server, "phala_container_id", new=AsyncMock(return_value="container-id")),
            patch.object(server, "resolve_docker_bin", return_value="docker"),
            patch.object(server, "run_subprocess", new=AsyncMock(return_value={"returncode": 0})),
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
        self.assertEqual(post.call_args.args[1]["method"], "anvil_reset")
        self.assertEqual(result["contract_state"], contract_state)
        self.assertEqual(result["phala_workers"]["deployed_worker_count"], 0)


if __name__ == "__main__":
    unittest.main()
