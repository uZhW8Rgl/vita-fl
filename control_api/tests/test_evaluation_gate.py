from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from control_api import server
from control_api.evaluation_gate import GateError, UploadGate

KEY = "0x" + "11" * 32
AGGREGATOR = Account.from_key(KEY).address.lower()
ROSTER = [AGGREGATOR] + ["0x" + f"{i:040x}" for i in range(1, 6)]
RUN = "a" * 32


class GateStateTests(unittest.TestCase):
    def test_generation_roster_selection_release_and_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "gate.json"
            gate = UploadGate(path)
            gate.arm(RUN, 1, 2, 6)
            gate.bind_roster(ROSTER)
            before = path.read_bytes()
            for member in ROSTER[1:]:
                result = gate.decision(RUN, 1, AGGREGATOR, member, chain_round=1, chain_aggregator=AGGREGATOR)
                self.assertEqual(result["allow"], member in ROSTER[1:3])
            self.assertEqual(path.read_bytes(), before)  # Public GET decisions never audit/mutate.
            with self.assertRaises(GateError):
                gate.decision("b" * 32, 1, AGGREGATOR, ROSTER[1], chain_round=1, chain_aggregator=AGGREGATOR)
            with self.assertRaises(GateError):
                gate.release("b" * 32)
            with self.assertRaises(GateError):
                gate.reset()
            gate.record_block(
                AGGREGATOR,
                {"run_id": RUN, "round": 1, "participant": ROSTER[-1]},
                chain_round=1,
                chain_aggregator=AGGREGATOR,
            )
            recovered = UploadGate(path)
            self.assertEqual(recovered.status()["blocked_counts"], {ROSTER[-1]: 1})
            self.assertEqual(recovered.status()["allowed_participants"], ROSTER[1:3])
            self.assertTrue(
                recovered.decision(RUN, 2, AGGREGATOR, ROSTER[-1], chain_round=2, chain_aggregator=AGGREGATOR)["allow"]
            )
            self.assertTrue(recovered.release(RUN)["released"])
            self.assertTrue(recovered.release(RUN)["released"])
            self.assertTrue(
                recovered.decision(RUN, 1, AGGREGATOR, ROSTER[-1], chain_round=1, chain_aggregator=AGGREGATOR)["allow"]
            )
            recovered.reset()
            self.assertFalse(path.exists())

    def test_signed_ready_observation_preserves_original_aggregator_after_abort(self):
        with TemporaryDirectory() as directory:
            gate = UploadGate(Path(directory) / "gate.json")
            gate.arm(RUN, 1, 2, 6)
            gate.bind_roster(ROSTER)
            with self.assertRaises(GateError):
                gate.record_ready(ROSTER[1], {"run_id": RUN, "round": 1}, chain_round=1, chain_aggregator=AGGREGATOR)
            gate.record_ready(AGGREGATOR, {"run_id": RUN, "round": 1}, chain_round=1, chain_aggregator=AGGREGATOR)
            later = gate.status(chain_round=2, chain_aggregator=ROSTER[1])
            self.assertEqual(later["selected_aggregator"], AGGREGATOR)
            self.assertEqual(later["allowed_participants"], ROSTER[1:3])
            self.assertEqual(later["blocked_total"], 0)

    def test_forged_or_allowed_block_reports_do_not_change_audit(self):
        with TemporaryDirectory() as directory:
            gate = UploadGate(Path(directory) / "gate.json")
            gate.arm(RUN, 1, 2, 6)
            gate.bind_roster(ROSTER)
            for receiver, participant, round_number in [
                (ROSTER[1], ROSTER[-1], 1),
                (AGGREGATOR, ROSTER[1], 1),
                (AGGREGATOR, ROSTER[-1], 2),
            ]:
                with self.assertRaises(GateError):
                    gate.record_block(
                        receiver,
                        {"run_id": RUN, "round": round_number, "participant": participant},
                        chain_round=round_number,
                        chain_aggregator=AGGREGATOR,
                    )
            self.assertEqual(gate.status()["blocked_total"], 0)


class GateApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gate = UploadGate(Path(self.directory.name) / "gate.json")
        self.status = AsyncMock(return_value={"training_phase": "setup", "run_roster": {"committed": False}})
        for patcher in (
            patch.object(server, "_evaluation_gate", self.gate),
            patch.object(server, "phala_runtime_mode", return_value=True),
            patch.object(server, "current_training_runtime_status", self.status),
            patch.object(server, "read_training_config", return_value={"worker_count": 6}),
            patch.object(
                server, "evaluation_gate_chain_context", return_value={"chain_round": 1, "chain_aggregator": AGGREGATOR}
            ),
            patch.dict(
                server.os.environ, {"EVALUATION_GATES_ENABLED": "true", "CONTROL_ADMIN_TOKEN": "admin-test-token"}
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        server.reset_runtime_telemetry()

    async def request(self, method, path="", body=None, auth=True):
        headers = {"x-control-admin-token": "admin-test-token"} if auth else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            return await client.request(method, "/api/evaluation/upload-gate" + path, json=body, headers=headers)

    async def arm(self):
        reply = await self.request("POST", body={"operation": "arm", "run_id": RUN, "round": 1, "allowed_clients": 2})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.gate.bind_roster(ROSTER)

    async def test_disabled_auth_and_prearm_phase(self):
        body = {"operation": "arm", "run_id": RUN, "round": 1, "allowed_clients": 2}
        self.assertEqual((await self.request("POST", body=body, auth=False)).status_code, 401)
        with patch.dict(server.os.environ, {"EVALUATION_GATES_ENABLED": "false"}):
            self.assertEqual((await self.request("POST", body=body)).status_code, 404)
        self.status.return_value = {"training_phase": "training"}
        self.assertEqual((await self.request("POST", body=body)).status_code, 409)
        self.assertFalse(self.gate.path.exists())

    async def test_public_decision_read_only_and_signed_receiver_audit(self):
        await self.arm()
        query = f"/decision?run_id={RUN}&round=1&aggregator={AGGREGATOR}&participant={ROSTER[-1]}"
        before = self.gate.path.read_bytes()
        response = await self.request("GET", query, auth=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["allow"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.gate.path.read_bytes(), before)
        payload = {
            "version": 1,
            "account": AGGREGATOR,
            "device_id": "0",
            "event": "evaluation.upload_blocked",
            "attributes": {"run_id": RUN, "round": 1, "participant": ROSTER[-1]},
            "timestamp_unix_ms": int(time.time() * 1000),
            "nonce": "nonce-unique-value-0001",
        }
        signature = Account.sign_message(
            encode_defunct(text=server._canonical_telemetry_payload(payload)), KEY
        ).signature.hex()
        with patch.object(server, "_dynamic_worker_slots", return_value={AGGREGATOR: "0"}):
            server._record_telemetry(payload, signature)
            with self.assertRaises(ValueError):
                server._record_telemetry(payload, signature)
            payload["nonce"] = "nonce-unique-value-0002"
            payload["attributes"]["participant"] = ROSTER[1]
            with self.assertRaises(ValueError):
                server._record_telemetry(payload, signature)
        status = await self.request("GET")
        self.assertEqual(status.json()["blocked_counts"], {ROSTER[-1]: 1})
        self.assertEqual(status.json()["allowed_participants"], ROSTER[1:3])
        self.assertEqual((await self.request("GET", auth=False)).status_code, 401)
