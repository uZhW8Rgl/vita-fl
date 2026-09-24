from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import httpx

from control_api import server


class TrainingSubmissionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / "training.env"
        self.env_file.write_text(
            "ROUND=3\nEPOCH=1\nWORKER_COUNT=3\nCLIENT_LIMIT=2\nMODEL_SUBMISSION_DEADLINE_MS=45001\n",
            encoding="utf-8",
        )
        self.compose_file = Path(self.directory.name) / "compose.yml"
        self.compose_file.write_text("services:\n  VM-0:\n  VM-1:\n  VM-2:\n", encoding="utf-8")
        original_read = server.read_training_config
        original_write = server.write_training_config
        self.status = AsyncMock(return_value={"training_phase": "setup"})
        for replacement in (
            patch.object(server, "phala_runtime_mode", return_value=False),
            patch.object(server, "require_control_admin"),
            patch.object(server, "current_training_runtime_status", self.status),
            patch.object(
                server,
                "read_training_config",
                side_effect=lambda *_args, **_kwargs: original_read(self.env_file, self.compose_file),
            ),
            patch.object(
                server,
                "write_training_config",
                side_effect=lambda config: original_write(config, self.env_file),
            ),
            patch.dict(server.os.environ, {"MODEL_SUBMISSION_DEADLINE_MS": "60000"}),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    async def request(self, method: str, path: str, payload=None) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            return await client.request(method, path, json=payload)

    async def test_config_round_trip_preserves_deadline_and_legacy_payloads_in_both_modes(self) -> None:
        for phala in (False, True):
            with self.subTest(phala=phala), patch.object(server, "phala_runtime_mode", return_value=phala):
                before = await self.request("GET", "/api/training/config")
                self.assertEqual(before.status_code, 200)
                self.assertNotEqual(before.json()["model_submission_deadline_ms"], 60000)
                updated = await self.request("POST", "/api/training/config", {"model_submission_deadline_ms": 12345})
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(updated.json()["config"]["model_submission_deadline_ms"], 12345)
                self.assertEqual(server.read_env_values(self.env_file)["MODEL_SUBMISSION_DEADLINE_MS"], "12345")
                legacy = await self.request("POST", "/api/training/config", {"epoch": 2})
                self.assertEqual(legacy.status_code, 200, legacy.text)
                self.assertEqual(legacy.json()["config"]["model_submission_deadline_ms"], 12345)
                read = await self.request("GET", "/api/training/config")
                self.assertEqual(read.json()["model_submission_deadline_ms"], 12345)

    async def test_invalid_deadlines_are_rejected_before_persistence_or_start(self) -> None:
        initial = self.env_file.read_bytes()
        maximum = server.MAX_AGGREGATION_SUBMISSION_WINDOW_SECONDS * 1000
        with (
            patch.object(server, "start_training_services", new=AsyncMock()) as start,
            patch.object(server, "configure_default_aggregation_policy") as policy,
        ):
            for route in ("/api/training/config", "/api/training/start"):
                for value in (None, True, False, 0, -1, 1.5, "not-a-number", maximum + 1):
                    with self.subTest(route=route, value=value):
                        response = await self.request("POST", route, {"model_submission_deadline_ms": value})
                        self.assertEqual(response.status_code, 400, response.text)
            start.assert_not_called()
            policy.assert_not_called()
        self.assertEqual(self.env_file.read_bytes(), initial)

    async def test_deadline_api_accepts_one_millisecond_and_contract_maximum(self) -> None:
        for deadline in (1, server.MAX_AGGREGATION_SUBMISSION_WINDOW_SECONDS * 1000):
            with self.subTest(deadline=deadline):
                response = await self.request(
                    "POST", "/api/training/config", {"model_submission_deadline_ms": deadline}
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["config"]["model_submission_deadline_ms"], deadline)

    async def test_missing_legacy_setting_uses_environment_then_default(self) -> None:
        self.env_file.write_text("ROUND=3\nEPOCH=1\nWORKER_COUNT=3\nCLIENT_LIMIT=2\n", encoding="utf-8")
        for configured, expected in (("61000", 61000), ("", server.DEFAULT_MODEL_SUBMISSION_DEADLINE_MS)):
            with (
                self.subTest(configured=configured),
                patch.dict(server.os.environ, {"MODEL_SUBMISSION_DEADLINE_MS": configured}),
            ):
                response = await self.request("GET", "/api/training/config")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["model_submission_deadline_ms"], expected)

    async def test_local_start_forwards_and_persists_the_selected_deadline(self) -> None:
        with patch.object(server, "start_training_services", new=AsyncMock(return_value={})) as start:
            response = await self.request("POST", "/api/training/start", {"model_submission_deadline_ms": 73123})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(start.call_args.args[0]["model_submission_deadline_ms"], 73123)
        self.assertFalse(start.call_args.kwargs["resume_committed_roster"])
        self.assertEqual(response.json()["config"]["model_submission_deadline_ms"], 73123)
        self.assertEqual(server.read_env_values(self.env_file)["MODEL_SUBMISSION_DEADLINE_MS"], "73123")

    async def test_phala_start_uses_saved_deadline_when_legacy_client_omits_field(self) -> None:
        workers = [
            {"slot": slot, "worker": f"worker{slot}", "account_address": "0x" + f"{slot + 1:040x}"} for slot in range(3)
        ]
        controller = Mock()
        controller.status.return_value = {"deployed_worker_count": 0, "workers": []}
        controller.selected_account_addresses.return_value = [worker["account_address"] for worker in workers]
        controller.scale.return_value = {"deployed_worker_count": 3, "workers": workers}
        with (
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "phala_worker_controller", return_value=controller),
            patch.object(server, "configure_default_aggregation_policy", return_value={}) as policy,
            patch.object(server, "commit_run_roster", return_value={}),
            patch.object(server, "publish_bootstrap_recipient_declaration", return_value={}),
            patch.object(server, "reset_runtime_telemetry"),
            patch.object(server, "phala_runtime_status", new=AsyncMock(return_value={})),
        ):
            response = await self.request("POST", "/api/training/start", {})
        self.assertEqual(response.status_code, 200, response.text)
        policy.assert_called_once_with(2, model_submission_deadline_ms=45001)
        self.assertEqual(response.json()["config"]["model_submission_deadline_ms"], 45001)
        self.assertEqual(controller.preflight_scale.call_args.args[1]["model_submission_deadline_ms"], 45001)

    async def test_committed_start_retry_rejects_changed_deadline_in_both_modes(self) -> None:
        self.status.return_value = {"training_phase": "bootstrap", "run_roster": {"committed": True}}
        initial = self.env_file.read_bytes()
        with (
            patch.object(server, "start_training_services", new=AsyncMock()) as start,
            patch.object(server, "phala_worker_controller") as controller,
            patch.object(server, "configure_default_aggregation_policy") as policy,
        ):
            for phala in (False, True):
                with self.subTest(phala=phala), patch.object(server, "phala_runtime_mode", return_value=phala):
                    response = await self.request(
                        "POST", "/api/training/start", {"model_submission_deadline_ms": 46000}
                    )
                    self.assertEqual(response.status_code, 409, response.text)
            start.assert_not_called()
            controller.assert_not_called()
            policy.assert_not_called()
        self.assertEqual(self.env_file.read_bytes(), initial)

    async def test_exact_local_retry_preserves_deadline_without_rewriting_saved_config(self) -> None:
        self.status.return_value = {"training_phase": "bootstrap", "run_roster": {"committed": True}}
        initial = self.env_file.read_bytes()
        with patch.object(server, "start_training_services", new=AsyncMock(return_value={})) as start:
            response = await self.request("POST", "/api/training/start", {})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(start.call_args.args[0]["model_submission_deadline_ms"], 45001)
        self.assertTrue(start.call_args.kwargs["resume_committed_roster"])
        self.assertEqual(self.env_file.read_bytes(), initial)


if __name__ == "__main__":
    unittest.main()
