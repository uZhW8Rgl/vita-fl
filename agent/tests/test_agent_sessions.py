from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from agent.run_agent import AgentRuntime


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


class AgentSessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_state_is_cleared_when_chat_is_cancelled(self) -> None:
        runtime = AgentRuntime(args=None)
        session = runtime.create_session()
        runtime._chat_session = AsyncMock(side_effect=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await runtime.chat(session["id"], "test")

        self.assertFalse(session["busy"])


if __name__ == "__main__":
    unittest.main()
