from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from control_api import server


class PhalaRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_training_config_caps_selection_at_six_workers(self) -> None:
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
        self.assertEqual(config["max_worker_count"], 6)
        self.assertEqual(config["available_workers"][0], "worker0")
        self.assertEqual(config["available_workers"][-1], "worker5")

    def test_user_rounds_exclude_bootstrap_round(self) -> None:
        config = {"rounds": 3, "epoch": 2, "worker_count": 4, "client_limit": 3}

        translated = server.worker_runtime_training_config(config)
        translated_again = server.worker_runtime_training_config(translated)

        self.assertEqual(translated["rounds"], 4)
        self.assertEqual(translated["requested_training_rounds"], 3)
        self.assertEqual(translated["bootstrap_completion_count"], 1)
        self.assertEqual(translated["worker_target_completed_round_count"], 4)
        self.assertEqual(translated_again, translated)
        self.assertEqual(translated["epoch"], 2)
        self.assertEqual(config["rounds"], 3)

    def test_requested_rounds_key_wins_over_legacy_round_value(self) -> None:
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(
                server,
                "read_env_values",
                return_value={
                    "REQUESTED_TRAINING_ROUNDS": "24",
                    "ROUND": "25",
                    "EPOCH": "2",
                    "WORKER_COUNT": "6",
                    "CLIENT_LIMIT": "5",
                },
            ),
            patch.dict(server.os.environ, {"MAX_DYNAMIC_WORKERS": "6"}, clear=False),
        ):
            config = server.read_training_config(Path("/runtime/training.env"))

        self.assertEqual(config["rounds"], 24)
        self.assertEqual(config["requested_training_rounds"], 24)
        self.assertEqual(config["worker_target_completed_round_count"], 25)

    def test_training_config_persists_requested_rounds_separately(self) -> None:
        config = {
            "rounds": 24,
            "epoch": 2,
            "worker_count": 6,
            "client_limit": 5,
        }
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / "training.env"
            env_file.write_text("ROUND=99\nEPOCH=1\n", encoding="utf-8")
            with (
                patch.object(server, "phala_runtime_mode", return_value=True),
                patch.dict(server.os.environ, {"MAX_DYNAMIC_WORKERS": "6"}, clear=False),
            ):
                persisted = server.write_training_config(config, env_file)
            values = server.read_env_values(env_file)

        self.assertEqual(values["REQUESTED_TRAINING_ROUNDS"], "24")
        self.assertEqual(values["ROUND"], "24")
        self.assertEqual(persisted["rounds"], 24)
        self.assertEqual(persisted["worker_target_completed_round_count"], 25)

    def test_completed_round_count_uses_successful_completion_selector(self) -> None:
        env_values = {
            "RPC_URL": "http://anvil:8545",
            "GM_STORAGE_ADDRESS": "0x" + "33" * 20,
        }
        with patch.object(
            server,
            "_post_json",
            return_value={"result": hex(7)},
        ) as post_json:
            completed = server.read_chain_completed_round_count(env_values)

        self.assertEqual(completed, 7)
        payload = post_json.call_args.args[1]
        self.assertEqual(payload["method"], "eth_call")
        self.assertEqual(payload["params"][0]["data"], "0x1ecb4dcc")

    async def test_metrics_export_distinguishes_attempts_and_successful_rounds(self) -> None:
        with (
            patch.object(server, "runtime_contract_env_values", return_value={}),
            patch.object(server, "read_chain_round", return_value=36),
            patch.object(server, "read_chain_completed_round_count", return_value=1),
            patch.object(server, "read_current_aggregator", return_value={"vm": "VM-0"}),
            patch.object(server, "read_training_config", return_value={"rounds": 24}),
            patch.object(server, "_telemetry_snapshot", return_value=[]),
            patch.object(server, "read_evaluation_summary_records", return_value=[]),
            patch.object(server, "read_transaction_cost_records", return_value=[]),
            patch.object(server, "phala_runtime_mode", return_value=True),
        ):
            response = await server.metrics()

        rendered = response.body.decode("utf-8")
        self.assertIn("dfl_protocol_round 36", rendered)
        self.assertIn("dfl_completed_round_count 1", rendered)
        self.assertIn("dfl_successful_training_rounds 0", rendered)
        self.assertIn("dfl_aborted_round_attempts 35", rendered)
        self.assertIn("dfl_target_training_rounds 24", rendered)

    def test_phala_training_config_prefers_persisted_selection(self) -> None:
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(
                server,
                "read_env_values",
                return_value={
                    "ROUND": "9",
                    "EPOCH": "4",
                    "WORKER_COUNT": "7",
                    "CLIENT_LIMIT": "5",
                },
            ),
            patch.dict(
                server.os.environ,
                {
                    "MAX_DYNAMIC_WORKERS": "500",
                    "ROUND": "2",
                    "EPOCH": "1",
                    "WORKER_COUNT": "3",
                    "CLIENT_LIMIT": "2",
                },
                clear=False,
            ),
        ):
            config = server.read_training_config(Path("/runtime/training.env"))

        self.assertEqual(config["rounds"], 9)
        self.assertEqual(config["epoch"], 4)
        self.assertEqual(config["worker_count"], 6)
        self.assertEqual(config["client_limit"], 5)
        self.assertEqual(config["max_worker_count"], 6)

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

    def test_runtime_manifest_is_optional_metadata_for_aggregation_policy(self) -> None:
        policy_address = "0x" + "55" * 20
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "registry_address": "0x" + "11" * 20,
                "aggregator_address": "0x" + "22" * 20,
                "gm_storage_address": "0x" + "33" * 20,
                "aggregation_policy_address": policy_address,
                "chain_id": "31337",
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
                    "DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "11" * 20,
                    "DYNAMIC_WORKER_EXPECTED_AGGREGATOR_ADDRESS": "0x" + "22" * 20,
                    "DYNAMIC_WORKER_EXPECTED_GM_STORAGE_ADDRESS": "0x" + "33" * 20,
                    "DYNAMIC_WORKER_EXPECTED_CHAIN_ID": "31337",
                },
                clear=False,
            ),
            patch.object(server.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            values = server.runtime_contract_env_values()

        self.assertEqual(
            values["RUNTIME_MANIFEST_AGGREGATION_POLICY_ADDRESS"],
            policy_address,
        )
        self.assertEqual(values["RPC_URL"], "http://anvil:8545")
        self.assertIn("http://ipfs:5001/api/v0/files/read", urlopen.call_args.args[0].full_url)

    def test_phala_runtime_manifest_rejects_a_mismatched_contract_address(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "registry_address": "0x" + "99" * 20,
                "aggregator_address": "0x" + "22" * 20,
                "gm_storage_address": "0x" + "33" * 20,
                "chain_id": "31337",
            }
        ).encode("utf-8")
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.dict(
                server.os.environ,
                {
                    "RPC_URL": "http://anvil:8545",
                    "KUBO_API_URL": "http://ipfs:5001",
                    "DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "11" * 20,
                    "DYNAMIC_WORKER_EXPECTED_AGGREGATOR_ADDRESS": "0x" + "22" * 20,
                    "DYNAMIC_WORKER_EXPECTED_GM_STORAGE_ADDRESS": "0x" + "33" * 20,
                    "DYNAMIC_WORKER_EXPECTED_CHAIN_ID": "31337",
                },
                clear=False,
            ),
            patch.object(server.urllib.request, "urlopen", return_value=response),
        ):
            with self.assertRaisesRegex(RuntimeError, "REGISTRY_ADDRESS does not match"):
                server.runtime_contract_env_values()

    def test_phala_status_can_recover_expected_addresses_without_mutable_manifest(self) -> None:
        expected = {
            "DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "11" * 20,
            "DYNAMIC_WORKER_EXPECTED_AGGREGATOR_ADDRESS": "0x" + "22" * 20,
            "DYNAMIC_WORKER_EXPECTED_GM_STORAGE_ADDRESS": "0x" + "33" * 20,
            "DYNAMIC_WORKER_EXPECTED_CHAIN_ID": "31337",
            "RPC_URL": "http://anvil:8545",
            "KUBO_API_URL": "http://ipfs:5001",
        }
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.dict(server.os.environ, expected, clear=False),
            patch.object(
                server.urllib.request,
                "urlopen",
                side_effect=server.urllib.error.URLError("missing MFS manifest"),
            ),
        ):
            values = server.runtime_contract_env_values()

        self.assertEqual(
            values["REGISTRY_ADDRESS"],
            expected["DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS"],
        )
        self.assertEqual(
            values["AGGREGATOR_ADDRESS"],
            expected["DYNAMIC_WORKER_EXPECTED_AGGREGATOR_ADDRESS"],
        )
        self.assertEqual(
            values["GM_STORAGE_ADDRESS"],
            expected["DYNAMIC_WORKER_EXPECTED_GM_STORAGE_ADDRESS"],
        )
        self.assertNotIn("AGGREGATION_POLICY_ADDRESS", values)

    def test_setup_derives_policy_from_measured_gm_storage_without_manifest(self) -> None:
        registry = "0x" + "11" * 20
        aggregator = "0x" + "22" * 20
        gm_storage = "0x" + "33" * 20
        policy = "0x" + "44" * 20

        def address_word(address: str) -> str:
            return "0x" + "0" * 24 + address[2:]

        def ethereum_rpc(_rpc_url: str, method: str, params: list[object]):
            if method == "eth_chainId":
                return "0x7a69"
            if method == "eth_getCode":
                return "0x60006000"
            if method == "eth_call":
                selector = params[0]["data"]
                return {
                    f"0x{server._ethereum_function_selector('device_registry_address()')}":
                        address_word(registry),
                    f"0x{server._ethereum_function_selector('aggregator_selection_address()')}":
                        address_word(aggregator),
                    f"0x{server._ethereum_function_selector('aggregation_policy_address()')}":
                        address_word(policy),
                    f"0x{server._ethereum_function_selector('gmStorage()')}":
                        address_word(gm_storage),
                }[selector]
            self.fail(f"unexpected RPC method {method}")

        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(
                server,
                "runtime_contract_env_values",
                return_value={
                    "RPC_URL": "http://anvil:8545",
                    "REGISTRY_ADDRESS": registry,
                    "AGGREGATOR_ADDRESS": aggregator,
                    "GM_STORAGE_ADDRESS": gm_storage,
                },
            ),
            patch.dict(
                server.os.environ,
                {
                    "RPC_URL": "http://anvil:8545",
                    "DYNAMIC_WORKER_EXPECTED_CHAIN_ID": "31337",
                },
                clear=False,
            ),
            patch.object(server, "_ethereum_rpc", side_effect=ethereum_rpc),
        ):
            values = server.verified_setup_contract_env_values()

        self.assertEqual(values["AGGREGATION_POLICY_ADDRESS"].lower(), policy)
        self.assertEqual(values["VERIFIED_CHAIN_ID"], "31337")

    def test_setup_rejects_manifest_policy_not_linked_from_gm_storage(self) -> None:
        policy = "0x" + "44" * 20
        with (
            patch.object(server, "phala_runtime_mode", return_value=False),
            patch.object(
                server,
                "runtime_contract_env_values",
                return_value={
                    "RPC_URL": "http://anvil:8545",
                    "REGISTRY_ADDRESS": "0x" + "11" * 20,
                    "AGGREGATOR_ADDRESS": "0x" + "22" * 20,
                    "GM_STORAGE_ADDRESS": "0x" + "33" * 20,
                    "RUNTIME_MANIFEST_AGGREGATION_POLICY_ADDRESS": "0x" + "55" * 20,
                },
            ),
            patch.object(server, "_ethereum_rpc") as rpc,
        ):
            rpc.side_effect = [
                "0x7a69",
                "0x6000",
                "0x6000",
                "0x6000",
                "0x" + "0" * 24 + ("11" * 20),
                "0x" + "0" * 24 + ("22" * 20),
                "0x" + "0" * 24 + policy[2:],
                "0x6000",
                "0x" + "0" * 24 + ("33" * 20),
            ]
            with self.assertRaisesRegex(RuntimeError, "does not match GMStorage"):
                server.verified_setup_contract_env_values()

    def test_local_runtime_manifest_supplies_aggregation_policy_address(self) -> None:
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
            patch.object(server, "phala_runtime_mode", return_value=False),
            patch.object(
                server,
                "read_env_values",
                return_value={
                    "RPC_URL": "http://anvil:8545",
                    "KUBO_API": "http://ipfs:5001",
                    "W0_PRIVATE_KEY": "0x" + "11" * 32,
                },
            ),
            patch.object(server.urllib.request, "urlopen", return_value=response),
        ):
            values = server.runtime_contract_env_values()

        self.assertEqual(
            values["RUNTIME_MANIFEST_AGGREGATION_POLICY_ADDRESS"],
            policy_address,
        )
        self.assertEqual(values["RPC_URL"], "http://anvil:8545")

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
                "verified_setup_contract_env_values",
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

    def test_exact_existing_run_roster_commit_is_an_idempotent_retry(self) -> None:
        from eth_utils import keccak

        roster = [
            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
            "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        ]
        digest = "0x" + keccak(server._encode_address_array_argument(roster)).hex()
        owner = "0x" + "aa" * 20
        existing = {
            "address": "0x" + "11" * 20,
            "committed": True,
            "frozen": False,
            "digest": digest,
            "worker_count": 2,
            "registered_worker_count": 0,
            "roster": roster,
        }
        with (
            patch.object(
                server,
                "verified_setup_contract_env_values",
                return_value={
                    "RPC_URL": "http://anvil:8545",
                    "REGISTRY_ADDRESS": "0x" + "11" * 20,
                    "ETH_WALLET_PRIVATE_KEY": "0x" + "44" * 32,
                },
            ),
            patch.object(server, "_ethereum_account_address", return_value=owner),
            patch.object(server, "_ethereum_rpc", return_value="0x"),
            patch.object(server, "_rpc_address", return_value=owner),
            patch.object(
                server,
                "read_current_aggregator",
                return_value={"address": roster[0], "vm": "VM-0"},
            ),
            patch.object(server, "read_run_roster_state", return_value=existing),
            patch.object(server, "_sign_ethereum_transaction") as sign_transaction,
        ):
            result = server.commit_run_roster(roster)

        self.assertTrue(result["idempotent"])
        self.assertIsNone(result["transaction_hash"])
        self.assertEqual(result["roster"], roster)
        sign_transaction.assert_not_called()

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

        def select_addresses(_worker_count: int):
            operation_order.append("select")
            return [worker["account_address"] for worker in workers]

        def commit_roster(_addresses):
            operation_order.append("commit")
            return {"transaction_hash": "0x" + "33" * 32}

        def scale_workers(*_args):
            operation_order.append("scale")
            return worker_status

        def publish_recipients(_addresses):
            operation_order.append("publish")
            return {"status": "declared"}

        controller.preflight_scale.side_effect = preflight_workers
        controller.selected_account_addresses.side_effect = select_addresses
        controller.scale.side_effect = scale_workers
        controller.status.return_value = {
            "deployed_worker_count": 0,
            "workers": [],
        }
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
            patch.object(server, "commit_run_roster", side_effect=commit_roster),
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
            patch.object(
                server,
                "reset_runtime_telemetry",
                side_effect=lambda: operation_order.append("reset-telemetry"),
            ),
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(return_value={"training_phase": "setup"}),
            ),
            patch.object(
                server,
                "phala_runtime_status",
                new=AsyncMock(return_value={"training_started": True}),
            ),
        ):
            result = await server.start_training(Mock(), normalized)

        self.assertEqual(
            operation_order,
            [
                "preflight",
                "select",
                "reset-telemetry",
                "policy",
                "commit",
                "scale",
                "publish",
            ],
        )
        self.assertEqual(result["aggregation_policy"]["transaction_hash"], "0x" + "22" * 32)
        worker_config = server.worker_runtime_training_config(normalized)
        controller.preflight_scale.assert_called_once_with(2, worker_config)
        controller.scale.assert_called_once_with(2, worker_config)

    async def test_training_start_returns_capacity_conflict_before_roster_commit(self) -> None:
        normalized = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 25,
            "client_limit": 24,
        }
        controller = Mock()
        controller.preflight_scale.side_effect = server.WorkerCapacityError(
            "Phala workspace quota can create at most 0 additional workers, "
            "but 16 are required"
        )

        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(server, "write_training_config"),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(server, "configure_default_aggregation_policy") as configure_policy,
            patch.object(server, "commit_run_roster") as commit_roster,
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(return_value={"training_phase": "setup"}),
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.start_training(Mock(), normalized)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("at most 0 additional workers", raised.exception.detail)
        controller.selected_account_addresses.assert_not_called()
        configure_policy.assert_not_called()
        commit_roster.assert_not_called()
        controller.scale.assert_not_called()

    async def test_training_start_rejects_preexisting_phala_workers_before_commit(self) -> None:
        normalized = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 2,
            "client_limit": 1,
        }
        addresses = ["0x" + f"{slot + 1:040x}" for slot in range(2)]
        controller = Mock()
        controller.selected_account_addresses.return_value = addresses
        controller.status.return_value = {
            "deployed_worker_count": 1,
            "workers": [{"worker": "worker0", "account_address": addresses[0]}],
        }
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(server, "write_training_config"),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(server, "configure_default_aggregation_policy") as configure_policy,
            patch.object(server, "commit_run_roster") as commit_roster,
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(return_value={"training_phase": "setup"}),
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.start_training(Mock(), normalized)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("already has deployed workers", raised.exception.detail)
        configure_policy.assert_not_called()
        commit_roster.assert_not_called()
        controller.scale.assert_not_called()

    async def test_bootstrap_start_retry_reuses_only_the_exact_committed_roster(self) -> None:
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
        operation_order: list[str] = []
        controller = Mock()
        controller.preflight_scale.side_effect = lambda *_args: operation_order.append("preflight")
        controller.selected_account_addresses.side_effect = lambda _count: (
            operation_order.append("select")
            or [worker["account_address"] for worker in workers]
        )
        controller.scale.side_effect = lambda *_args: (
            operation_order.append("scale")
            or {"deployed_worker_count": 2, "workers": workers}
        )

        def commit_roster(_addresses):
            operation_order.append("commit")
            return {"idempotent": True, "digest": "0x" + "44" * 32}

        def publish_recipients(_addresses):
            operation_order.append("publish")
            return {"status": "declared"}

        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(server, "configure_default_aggregation_policy") as configure_policy,
            patch.object(server, "commit_run_roster", side_effect=commit_roster),
            patch.object(
                server,
                "publish_bootstrap_recipient_declaration",
                side_effect=publish_recipients,
            ),
            patch.object(server, "write_training_config") as write_config,
            patch.object(
                server,
                "read_training_config",
                return_value={**normalized, "max_worker_count": 500},
            ),
            patch.object(server, "reset_runtime_telemetry") as reset_telemetry,
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(
                    return_value={
                        "training_phase": "bootstrap",
                        "run_roster": {"committed": True},
                    }
                ),
            ),
            patch.object(
                server,
                "phala_runtime_status",
                new=AsyncMock(return_value={"training_phase": "bootstrap"}),
            ),
        ):
            result = await server.start_training(Mock(), normalized)

        self.assertEqual(operation_order, ["preflight", "select", "commit", "scale", "publish"])
        self.assertTrue(result["resumed_committed_roster"])
        reset_telemetry.assert_not_called()
        configure_policy.assert_not_called()
        write_config.assert_not_called()
        controller.status.assert_not_called()

    async def test_training_start_does_not_scale_after_policy_failure(self) -> None:
        normalized = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 2,
            "client_limit": 1,
        }
        controller = Mock()
        controller.status.return_value = {
            "deployed_worker_count": 0,
            "workers": [],
        }
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "require_control_admin"),
            patch.object(server, "normalize_training_config", return_value=normalized),
            patch.object(server, "write_training_config") as write_config,
            patch.object(
                server,
                "configure_default_aggregation_policy",
                side_effect=RuntimeError("policy transaction reverted"),
            ),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(return_value={"training_phase": "setup"}),
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.start_training(Mock(), normalized)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("policy transaction reverted", raised.exception.detail)
        write_config.assert_called_once_with(normalized)
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
            patch.object(server, "write_training_config") as write_config,
            patch.object(server, "configure_default_aggregation_policy") as configure_policy,
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(return_value={"training_phase": "setup"}),
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.start_training(Mock(), normalized)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("attested compose mismatch", raised.exception.detail)
        write_config.assert_called_once_with(normalized)
        configure_policy.assert_not_called()
        controller.scale.assert_not_called()

    async def test_local_start_removes_stale_workers_before_roster_commit(self) -> None:
        config = {
            "rounds": 3,
            "epoch": 2,
            "worker_count": 2,
            "client_limit": 1,
        }
        operation_order: list[str] = []

        async def reset_services(targets):
            operation_order.append("reset:" + ",".join(targets))
            return []

        def configure_policy(_client_limit):
            operation_order.append("policy")
            return {"transaction_hash": "0x" + "22" * 32}

        def commit_roster(_addresses):
            operation_order.append("commit")
            return {"idempotent": False, "digest": "0x" + "44" * 32}

        def publish_recipients(_addresses):
            operation_order.append("publish")
            return {"status": "declared"}

        with (
            patch.object(server, "_available_worker_services", return_value=["VM-0", "VM-1"]),
            patch.object(
                server,
                "collect_runtime_status",
                new=AsyncMock(return_value={"contract_initialized": True}),
            ),
            patch.object(server, "local_worker_addresses", return_value=["0x" + "01" * 20, "0x" + "02" * 20]),
            patch.object(server, "reset_services", side_effect=reset_services),
            patch.object(server, "reset_observability_state", new=AsyncMock(return_value=[])),
            patch.object(server, "configure_default_aggregation_policy", side_effect=configure_policy),
            patch.object(server, "commit_run_roster", side_effect=commit_roster),
            patch.object(server, "use_local_ollama", return_value=False),
            patch.object(server, "run_subprocess", new=AsyncMock(return_value={"returncode": 0})),
            patch.object(server, "wait_for_service_ready", new=AsyncMock(return_value={})),
            patch.object(server, "publish_bootstrap_recipient_declaration", side_effect=publish_recipients),
        ):
            result = await server.start_training_services(config)

        self.assertEqual(operation_order[0], "reset:agent,zk-inference,VM-0,VM-1")
        self.assertLess(operation_order.index("policy"), operation_order.index("commit"))
        self.assertLess(operation_order.index("commit"), operation_order.index("publish"))
        self.assertFalse(result["resumed_committed_roster"])

    async def test_generic_worker_scale_cannot_create_a_training_roster(self) -> None:
        controller = Mock()
        with (
            patch.object(server, "require_control_admin"),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(
                server,
                "current_training_runtime_status",
                new=AsyncMock(return_value={"training_phase": "setup"}),
            ),
        ):
            with self.assertRaises(server.HTTPException) as raised:
                await server.scale_phala_workers(
                    Mock(),
                    {"worker_count": 2},
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Start Training", raised.exception.detail)
        controller.scale.assert_not_called()

    def test_bootstrap_recipient_declaration_uses_on_chain_authority(self) -> None:
        first = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        second = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        registry_address = "0x" + "11" * 20

        def ethereum_rpc(_rpc_url: str, method: str, _params: list[object]):
            if method == "eth_chainId":
                return "0x7a69"
            if method == "eth_call":
                return "0x" + "22" * 32
            self.fail(f"unexpected RPC method {method}")

        with (
            patch.object(
                server,
                "runtime_contract_env_values",
                return_value={
                    "RPC_URL": "http://anvil:8545",
                    "REGISTRY_ADDRESS": registry_address,
                },
            ),
            patch.object(server, "_ethereum_rpc", side_effect=ethereum_rpc),
            patch.object(
                server,
                "read_run_roster_state",
                return_value={
                    "committed": True,
                    "digest": "0x" + "44" * 32,
                    "roster": [first, second],
                },
            ),
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
        self.assertEqual(declaration["registry_address"], registry_address)
        self.assertEqual(
            declaration["expected_worker_image_digest"],
            "0x" + "22" * 32,
        )
        self.assertEqual(
            declaration["recipients"],
            [first.lower(), second.lower()],
        )
        self.assertEqual(declaration["worker_count"], 2)
        self.assertEqual(declaration["bootstrap_worker"], first.lower())
        self.assertEqual(
            declaration["security_authority"],
            "DeviceRegistry.runRosterDigest",
        )
        self.assertEqual(declaration["onchain_roster_digest"], "0x" + "44" * 32)
        self.assertRegex(declaration["roster_sha256"], r"^[a-f0-9]{64}$")
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
        admission_state = {
            "status": "running",
            "phase": "admission-ready",
            "admission_marker": {"status": "admission-ready"},
        }
        exit_state = {
            "status": "exited",
            "running": False,
            "exit_code": 0,
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
                new=AsyncMock(return_value=admission_state),
            ),
            patch.object(
                server,
                "wait_for_docker_container_exit_success",
                new=AsyncMock(return_value=exit_state),
            ) as wait_for_exit,
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
        wait_for_exit.assert_awaited_once_with(
            "contract-container-id",
            "smart-contracts",
            server.CONTRACT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            result["contract_state"],
            {
                **exit_state,
                "phase": "deployed",
                "admission_marker": {"status": "admission-ready"},
            },
        )
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

    async def test_runtime_admission_marker_remains_valid_after_clean_exit(self) -> None:
        state = {
            "status": "exited",
            "running": False,
            "exit_code": 0,
        }
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

        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["phase"], "admission-ready")

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

    def test_training_lifecycle_uses_worker_and_on_chain_round_state(self) -> None:
        self.assertEqual(
            server.training_lifecycle_phase(
                contracts_ready=True,
                worker_count=0,
                current_round=0,
                completed_round_count=0,
                run_roster_committed=False,
            ),
            "setup",
        )
        self.assertEqual(
            server.training_lifecycle_phase(
                contracts_ready=True,
                worker_count=3,
                current_round=0,
                completed_round_count=0,
                run_roster_committed=True,
            ),
            "bootstrap",
        )
        self.assertEqual(
            server.training_lifecycle_phase(
                contracts_ready=True,
                worker_count=3,
                current_round=1,
                completed_round_count=1,
                run_roster_committed=True,
            ),
            "training",
        )
        self.assertEqual(
            server.training_lifecycle_phase(
                contracts_ready=True,
                worker_count=0,
                current_round=2,
                completed_round_count=1,
                target_completed_round_count=4,
                run_roster_committed=True,
            ),
            "training",
        )
        self.assertEqual(
            server.training_lifecycle_phase(
                contracts_ready=True,
                worker_count=0,
                current_round=4,
                completed_round_count=4,
                target_completed_round_count=4,
                run_roster_committed=True,
            ),
            "completed",
        )
        self.assertEqual(
            server.training_lifecycle_phase(
                contracts_ready=True,
                worker_count=3,
                current_round=4,
                completed_round_count=3,
                target_completed_round_count=4,
                run_roster_committed=True,
            ),
            "training",
        )

    async def test_phala_status_reports_bootstrap_until_round_one(self) -> None:
        controller = Mock()
        controller.status.return_value = {
            "deployed_worker_count": 2,
            "workers": [
                {"worker": "worker0"},
                {"worker": "worker1"},
            ],
        }
        with (
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(
                server,
                "runtime_contract_env_values",
                return_value={"RPC_URL": "http://anvil:8545"},
            ),
            patch.object(server, "probe_anvil_ready", return_value=True),
            patch.object(server, "probe_ipfs_ready", return_value=True),
            patch.object(server, "probe_contract_deployment", return_value=True),
            patch.object(server, "runtime_admission_is_ready", return_value=False),
            patch.object(
                server,
                "phala_container_id",
                new=AsyncMock(return_value="contract-container"),
            ),
            patch.object(
                server,
                "inspect_docker_container",
                new=AsyncMock(
                    return_value={
                        "service": "smart-contracts",
                        "status": "exited",
                        "running": False,
                        "exit_code": 0,
                    }
                ),
            ),
            patch.object(
                server,
                "training_target_completed_round_count",
                return_value=4,
            ),
            patch.object(server, "read_chain_round", return_value=0) as read_round,
            patch.object(
                server,
                "read_chain_completed_round_count",
                return_value=0,
            ) as read_completed_round_count,
            patch.object(
                server,
                "read_run_roster_state",
                return_value={
                    "committed": True,
                    "frozen": False,
                    "digest": "0x" + "44" * 32,
                    "roster": ["0x" + "01" * 20, "0x" + "02" * 20],
                },
            ),
            patch.object(
                server,
                "read_current_aggregator",
                return_value={"address": None, "vm": None},
            ),
            patch.object(server, "environment_flag", return_value=False),
        ):
            bootstrap = await server.phala_runtime_status()
            read_round.return_value = 1
            read_completed_round_count.return_value = 1
            training = await server.phala_runtime_status()
            read_round.return_value = 4
            read_completed_round_count.return_value = 4
            completed = await server.phala_runtime_status()

        self.assertEqual(bootstrap["training_phase"], "bootstrap")
        self.assertTrue(bootstrap["contract_initialized"])
        self.assertFalse(bootstrap["contract_admission_ready"])
        self.assertTrue(bootstrap["bootstrap_in_progress"])
        self.assertFalse(bootstrap["training_started"])
        self.assertEqual(training["training_phase"], "training")
        self.assertTrue(training["bootstrap_completed"])
        self.assertTrue(training["training_started"])
        self.assertEqual(completed["training_phase"], "completed")
        self.assertTrue(completed["training_completed"])
        self.assertTrue(completed["training_started"])

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
