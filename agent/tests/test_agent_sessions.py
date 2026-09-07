from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent.run_agent import AgentRuntime, _local_langchain_tools


class AgentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = AgentRuntime(args=None)

    def test_only_one_empty_draft_session_is_kept(self) -> None:
        first = self.runtime.create_session()
        second = self.runtime.create_session()

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([item["id"] for item in self.runtime.list_sessions()], [second["id"]])

    def test_sessions_are_listed_newest_activity_first(self) -> None:
        first = self.runtime.create_session()
        first["messages"].append({"role": "user", "content": "first"})
        first["updated_at"] = "2026-01-01T00:00:00+00:00"
        second = self.runtime.create_session()
        second["messages"].append({"role": "user", "content": "second"})
        second["updated_at"] = "2026-01-02T00:00:00+00:00"

        sessions = self.runtime.list_sessions()

        self.assertEqual([item["id"] for item in sessions], [second["id"], first["id"]])
        self.assertFalse(sessions[0]["busy"])

    def test_legacy_zk_skill_names_route_to_remote_public_tools(self) -> None:
        aliases = {
            "fetch_latest_verified_model_bundle": "fetch_latest_verified_zk_model_bundle",
            "generate_random_chestmnist_image": "generate_random_zk_chestmnist_image",
            "generate_zk_inference_proof": "generate_and_verify_zk_inference_proof",
        }
        for requested, expected in aliases.items():
            with self.subTest(requested=requested):
                self.assertEqual(
                    self.runtime._infer_requested_skill(requested),
                    expected,
                )

    def test_bundle_commands_preserve_explicit_inference_mode(self) -> None:
        commands = {
            "fetch the latest bundle": "tee",
            "Please fetch the latest verified TEE model bundle.": "tee",
            "get the current model bundle, please!": "tee",
            "fetch the latest ZK bundle": "zk",
            "fetch the latest bundle inside the ZK TEE": "zk",
            "download the verified EZKL model bundle": "zk",
        }
        for command, mode in commands.items():
            with self.subTest(command=command):
                expected = f"fetch_latest_verified_{mode}_model_bundle"
                self.assertEqual(self.runtime._bundle_requested_skill(command), expected)
                self.assertEqual(self.runtime._infer_requested_skill(command), expected)

    def test_disabled_zk_is_absent_from_planner_tools(self) -> None:
        with patch.dict("os.environ", {"ZK_INFERENCE_ENABLED": "false"}):
            tools = _local_langchain_tools()
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "fetch_latest_verified_tee_model_bundle",
                "generate_random_tee_chestmnist_image",
                "run_and_verify_tee_inference",
            },
        )

    def test_enabled_zk_keeps_both_planner_workflows(self) -> None:
        with patch.dict("os.environ", {"ZK_INFERENCE_ENABLED": "true"}):
            tools = _local_langchain_tools()
        self.assertEqual(len(tools), 6)
        self.assertIn("fetch_latest_verified_zk_model_bundle", {tool.name for tool in tools})


class AgentSessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_state_is_cleared_when_chat_is_cancelled(self) -> None:
        runtime = AgentRuntime(args=None)
        session = runtime.create_session()
        runtime._chat_session = AsyncMock(side_effect=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await runtime.chat(session["id"], "test")

        self.assertFalse(session["busy"])

    async def test_exact_skill_bypasses_llm_planner(self) -> None:
        runtime = AgentRuntime(args=None)
        session = runtime.create_session()
        expected = runtime._assistant_response(
            "Tool completed.",
            [{"type": "tool", "label": "fetch_latest_verified_tee_model_bundle", "detail": "{}"}],
        )
        runtime.ensure_agent_model = AsyncMock(side_effect=AssertionError("LLM planner must not run"))

        with patch.object(runtime, "_execute_skill_fallback", return_value=expected) as execute:
            updated = await runtime.chat(session["id"], "fetch_latest_verified_tee_model_bundle")

        execute.assert_called_once_with(session["state"], "fetch_latest_verified_tee_model_bundle")
        self.assertEqual(updated["messages"][-1]["content"], "Tool completed.")
        self.assertFalse(updated["busy"])

    async def test_bundle_commands_call_requested_receiver_without_planner(self) -> None:
        for command, mode in (
            ("fetch the latest bundle", "tee"),
            ("fetch the latest ZK bundle", "zk"),
        ):
            with self.subTest(command=command):
                runtime = AgentRuntime(args=None)
                session = runtime.create_session()
                runtime.ensure_agent_model = AsyncMock(side_effect=AssertionError("LLM planner must not run"))
                payload = {"ok": True, "tool_receipt": {"status": "verified"}}
                receivers = SimpleNamespace(
                    fetch_latest_verified_tee_model_bundle=Mock(return_value=json.dumps(payload)),
                    fetch_latest_verified_zk_model_bundle=Mock(return_value=json.dumps(payload)),
                )
                with patch.dict("sys.modules", {"mcp_server": receivers}):
                    updated = await runtime.chat(session["id"], command)

                selected = getattr(receivers, f"fetch_latest_verified_{mode}_model_bundle")
                other_mode = "zk" if mode == "tee" else "tee"
                unselected = getattr(receivers, f"fetch_latest_verified_{other_mode}_model_bundle")
                selected.assert_called_once_with()
                unselected.assert_not_called()
                self.assertEqual(session["state"][f"latest_{mode}_model"], payload)
                event = updated["messages"][-1]["events"][0]
                self.assertEqual(event["label"], f"fetch_latest_verified_{mode}_model_bundle")
                self.assertEqual(json.loads(event["detail"])["tool_receipt"], payload["tool_receipt"])

    async def test_bundle_discussion_and_multistep_requests_stay_with_planner(self) -> None:
        commands = (
            "How do I fetch the latest bundle?",
            "fetch the latest bundle?",
            "Do not fetch the latest bundle.",
            "Don't fetch the latest bundle; explain it.",
            "fetch the latest bundle and run inference",
            "fetch the latest bundle and run TEE inference",
            "fetch the latest bundle, then generate a proof",
            "fetch the latest TEE bundle inside the ZK TEE",
        )
        for command in commands:
            with self.subTest(command=command):
                runtime = AgentRuntime(args=None)
                session = runtime.create_session()
                runtime.ensure_agent_model = AsyncMock()
                runtime._run_agent_with_timeout = AsyncMock(
                    return_value=({"output": "Planner response."}, False, False)
                )
                with patch.object(runtime, "_execute_skill_fallback") as execute:
                    updated = await runtime.chat(session["id"], command)

                runtime.ensure_agent_model.assert_awaited_once()
                runtime._run_agent_with_timeout.assert_awaited_once()
                execute.assert_not_called()
                self.assertEqual(updated["messages"][-1]["content"], "Planner response.")

    async def test_explicit_zk_failure_never_switches_to_tee(self) -> None:
        runtime = AgentRuntime(args=None)
        session = runtime.create_session()
        runtime.ensure_agent_model = AsyncMock(side_effect=AssertionError("LLM planner must not run"))
        receivers = SimpleNamespace(
            fetch_latest_verified_tee_model_bundle=Mock(),
            fetch_latest_verified_zk_model_bundle=Mock(side_effect=RuntimeError("ZK inference is disabled")),
        )
        with patch.dict("sys.modules", {"mcp_server": receivers}):
            updated = await runtime.chat(session["id"], "fetch the latest ZK bundle")

        receivers.fetch_latest_verified_zk_model_bundle.assert_called_once_with()
        receivers.fetch_latest_verified_tee_model_bundle.assert_not_called()
        self.assertIn("ZK inference is disabled", updated["messages"][-1]["content"])
        self.assertNotIn("latest_zk_model", session["state"])
        self.assertNotIn("latest_tee_model", session["state"])

    async def test_requested_target_tool_closes_agent_stream_before_llm_tail(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.items = iter(
                    [
                        {"messages": [{"type": "tool", "name": "preparation_tool", "content": "{}"}]},
                        {
                            "messages": [
                                {
                                    "type": "tool",
                                    "name": "run_and_verify_tee_inference",
                                    "content": '{"ok":true}',
                                }
                            ]
                        },
                        {"messages": [{"type": "assistant", "content": "slow LLM tail"}]},
                    ]
                )
                self.closed = False
                self.yielded = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    item = next(self.items)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc
                self.yielded += 1
                return item

            async def aclose(self) -> None:
                self.closed = True

        class Executor:
            def __init__(self, stream: Stream) -> None:
                self.stream = stream

            def astream(self, *_args, **_kwargs):
                return self.stream

        runtime = AgentRuntime(args=None)
        runtime._agent_backend = "langchain_v1"
        stream = Stream()
        runtime._agent_executor = Executor(stream)

        response, timed_out, completed = await runtime._run_agent_with_timeout(
            [],
            {},
            timeout_seconds=1,
            target_skill="run_and_verify_tee_inference",
        )

        self.assertFalse(timed_out)
        self.assertTrue(completed)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.yielded, 2)
        self.assertEqual(response["messages"][0]["name"], "run_and_verify_tee_inference")

    async def test_unrelated_tool_does_not_close_agent_stream(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.items = iter(
                    [
                        {"messages": [{"type": "tool", "name": "preparation_tool", "content": "{}"}]},
                        {"messages": [{"type": "assistant", "content": "done"}]},
                    ]
                )
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

            async def aclose(self) -> None:
                self.closed = True

        class Executor:
            def __init__(self, stream: Stream) -> None:
                self.stream = stream

            def astream(self, *_args, **_kwargs):
                return self.stream

        runtime = AgentRuntime(args=None)
        runtime._agent_backend = "langchain_v1"
        stream = Stream()
        runtime._agent_executor = Executor(stream)

        response, timed_out, completed = await runtime._run_agent_with_timeout(
            [],
            {},
            timeout_seconds=1,
            target_skill="run_and_verify_tee_inference",
        )

        self.assertFalse(timed_out)
        self.assertFalse(completed)
        self.assertFalse(stream.closed)
        self.assertEqual(response["messages"][0]["content"], "done")


if __name__ == "__main__":
    unittest.main()
