from __future__ import annotations

import unittest

from agent import mcp_server

EXPECTED_TOOLS = {
    "fetch_latest_verified_tee_model_bundle",
    "generate_random_tee_chestmnist_image",
    "run_and_verify_tee_inference",
    "fetch_latest_verified_zk_model_bundle",
    "generate_random_zk_chestmnist_image",
    "generate_and_verify_zk_inference_proof",
}


class McpSurfaceTests(unittest.TestCase):
    def test_only_the_six_job_tools_are_public(self) -> None:
        self.assertEqual(set(mcp_server.PUBLIC_MCP_TOOL_NAMES), EXPECTED_TOOLS)
        self.assertEqual(set(mcp_server.mcp._tool_manager._tools), EXPECTED_TOOLS)


if __name__ == "__main__":
    unittest.main()
