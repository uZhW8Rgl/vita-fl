from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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

    def test_training_config_requires_an_aggregator_and_at_least_one_client(self) -> None:
        current = {
            "rounds": 3,
            "epoch": 1,
            "worker_count": 3,
            "client_limit": 2,
            "max_worker_count": 500,
        }
        with patch.object(server, "read_training_config", return_value=current):
            normalized = server.normalize_training_config(
                {"worker_count": 1, "client_limit": 1}
            )

        self.assertEqual(normalized["worker_count"], 2)
        self.assertEqual(normalized["client_limit"], 1)

    def test_runtime_manifest_exposes_aggregation_policy_address(self) -> None:
        policy_address = "0x" + "55" * 20
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "registry_address": "0x" + "11" * 20,
                "aggregator_address": "0x" + "22" * 20,
                "gm_storage_address": "0x" + "33" * 20,
                "aggregation_policy_address": policy_address,
            }
        ).encode("utf-8")
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.dict(
                server.os.environ,
                {
                    "RPC_URL": "http://anvil:8545",
                    "DYNAMIC_WORKER_RPC_URL": "https://external-rpc.example",
                    "KUBO_API_URL": "http://ipfs:5001",
                    "DYNAMIC_WORKER_KUBO_API_URL": "https://external-kubo.example",
                },
                clear=False,
            ),
            patch.object(server.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            values = server.runtime_contract_env_values()

        self.assertEqual(values["AGGREGATION_POLICY_ADDRESS"], policy_address)
        self.assertEqual(values["RPC_URL"], "http://anvil:8545")
        self.assertIn("http://ipfs:5001/api/v0/files/read", urlopen.call_args.args[0].full_url)

    def test_default_policy_configuration_is_signed_and_uses_ceiling_seconds(self) -> None:
        policy_address = "0x610178da211fef7d417bc0e6fed39f05609ad788"
        checksummed_policy_address = "0x610178dA211FEF7D417bC0e6FeD39F05609AD788"
        owner_address = "0x" + "66" * 20
        transaction_hash = "0x" + "77" * 32
        rpc_calls: list[tuple[str, str, list[object]]] = []

        def ethereum_rpc(rpc_url: str, method: str, params: list[object]):
            rpc_calls.append((rpc_url, method, params))
            if method == "eth_call":
                selector = params[0]["data"]
                return {
                    f"0x{server.AGGREGATION_POLICY_OWNER_SELECTOR}":
                        "0x" + "0" * 24 + owner_address[2:],
                    f"0x{server.AGGREGATION_POLICY_REQUIRED_SUBMISSIONS_SELECTOR}":
                        f"0x{3:064x}",
                    f"0x{server.AGGREGATION_POLICY_SUBMISSION_WINDOW_SELECTOR}":
                        f"0x{21:064x}",
                }[selector]
            return {
                "eth_chainId": "0x7a69",
                "eth_getTransactionCount": "0x4",
                "eth_gasPrice": "0x3b9aca00",
                "eth_estimateGas": "0xc350",
                "eth_sendRawTransaction": transaction_hash,
                "eth_getTransactionReceipt": {
                    "status": "0x1",
                    "blockNumber": "0x9",
                    "gasUsed": "0xa410",
                },
            }[method]

        with (
            patch.object(
                server,
                "runtime_contract_env_values",
                return_value={
                    "RPC_URL": "https://external-rpc.example",
                    "AGGREGATION_POLICY_ADDRESS": policy_address,
                },
            ),
            patch.dict(
                server.os.environ,
                {
                    "RPC_URL": "http://anvil:8545",
                    "ETH_WALLET_PRIVATE_KEY": "0x" + "88" * 32,
                    "MODEL_SUBMISSION_DEADLINE_MS": "20001",
                },
                clear=False,
            ),
            patch.object(server, "_ethereum_rpc", side_effect=ethereum_rpc),
            patch.object(
                server,
                "_ethereum_account_address",
                return_value=owner_address,
            ),
            patch.object(
                server,
                "_sign_ethereum_transaction",
                return_value="0xdeadbeef",
            ) as sign_transaction,
        ):
            result = server.configure_default_aggregation_policy(3)

        transaction = sign_transaction.call_args.args[0]
        expected_arguments = (3).to_bytes(32, "big") + (21).to_bytes(32, "big")
        self.assertEqual(
            transaction["data"],
            f"0x{server.AGGREGATION_POLICY_CONFIGURE_SELECTOR}{expected_arguments.hex()}",
        )
        self.assertEqual(transaction["to"], checksummed_policy_address)
        self.assertEqual(transaction["chainId"], 31337)
        self.assertEqual(transaction["nonce"], 4)
        self.assertEqual(transaction["gas"], 60_000)
        self.assertEqual(result["address"], checksummed_policy_address)
        self.assertEqual(result["submission_window_seconds"], 21)
        self.assertEqual(result["transaction_hash"], transaction_hash)
        self.assertTrue(all(call[0] == "http://anvil:8545" for call in rpc_calls))
        contract_calls = [
            params[0]
            for _rpc_url, method, params in rpc_calls
            if method in {"eth_call", "eth_estimateGas"}
        ]
        self.assertTrue(contract_calls)
        self.assertTrue(
            all(call["to"] == checksummed_policy_address for call in contract_calls)
        )

    def test_checksum_ethereum_address_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not a valid Ethereum address"):
            server._checksum_ethereum_address("0x1234", "policy")

    def test_eth_account_signer_supports_the_control_api_transaction_shape(self) -> None:
        private_key = (
            "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        )
        address = server._ethereum_account_address(private_key)

        raw_transaction = server._sign_ethereum_transaction(
            {
                "chainId": 31337,
                "nonce": 0,
                "to": server._checksum_ethereum_address(
                    "0x610178da211fef7d417bc0e6fed39f05609ad788",
                    "policy",
                ),
                "value": 0,
                "data": "0x95b40727" + "00" * 64,
                "gas": 100_000,
                "gasPrice": 1_000_000_000,
            },
            private_key,
        )

        self.assertEqual(
            address,
            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        )
        self.assertRegex(raw_transaction, r"^0x[a-fA-F0-9]+$")

    async def test_training_start_preflights_then_configures_policy_before_worker_scale(self) -> None:
        normalized = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 2,
            "client_limit": 1,
        }
        workers = [
            {
                "slot": slot,
                "worker": f"worker{slot}",
                "account_address": "0x" + f"{slot + 1:040x}",
            }
            for slot in range(2)
        ]
        worker_status = {
            "deployed_worker_count": 2,
            "workers": workers,
        }
        operation_order: list[str] = []
        controller = Mock()

        def preflight_workers(*_args):
            operation_order.append("preflight")

        def configure_policy(_client_limit: int):
            operation_order.append("policy")
            return {"transaction_hash": "0x" + "22" * 32}

        def scale_workers(*_args):
            operation_order.append("scale")
            return worker_status

        def publish_recipients(_addresses):
            operation_order.append("publish")
            return {"status": "declared"}

        controller.preflight_scale.side_effect = preflight_workers
        controller.scale.side_effect = scale_workers
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(
                server,
                "configure_default_aggregation_policy",
                side_effect=configure_policy,
            ),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(
                server,
                "publish_bootstrap_recipient_declaration",
                side_effect=publish_recipients,
            ),
            patch.object(server, "write_training_config"),
            patch.object(
                server,
                "read_training_config",
                return_value={**normalized, "max_worker_count": 500},
            ),
            patch.object(server, "reset_runtime_telemetry"),
            patch.object(
                server,
                "phala_runtime_status",
                new=AsyncMock(return_value={"training_started": True}),
            ),
        ):
            result = await server.start_training(Mock(), normalized)

        self.assertEqual(operation_order, ["preflight", "policy", "scale", "publish"])
        self.assertEqual(result["aggregation_policy"]["transaction_hash"], "0x" + "22" * 32)
        controller.preflight_scale.assert_called_once_with(
            2,
            {**normalized, "rounds": 4},
        )
        controller.scale.assert_called_once_with(
            2,
            {**normalized, "rounds": 4},
        )

    async def test_training_start_does_not_scale_after_policy_failure(self) -> None:
        normalized = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 2,
            "client_limit": 1,
        }
        controller = Mock()
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(
                server,
                "configure_default_aggregation_policy",
                side_effect=RuntimeError("policy transaction reverted"),
            ),
            patch.object(server, "phala_worker_controller", return_value=controller),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.start_training(Mock(), normalized)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("policy transaction reverted", raised.exception.detail)
        controller.preflight_scale.assert_called_once()
        controller.scale.assert_not_called()

    async def test_training_start_does_not_change_policy_after_scale_preflight_failure(self) -> None:
        normalized = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 2,
            "client_limit": 1,
        }
        controller = Mock()
        controller.preflight_scale.side_effect = RuntimeError("attested compose mismatch")
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(server, "configure_default_aggregation_policy") as configure_policy,
            patch.object(server, "phala_worker_controller", return_value=controller),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.start_training(Mock(), normalized)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("attested compose mismatch", raised.exception.detail)
        configure_policy.assert_not_called()
        controller.scale.assert_not_called()

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
