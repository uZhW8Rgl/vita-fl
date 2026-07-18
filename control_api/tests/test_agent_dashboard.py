from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


EXPECTED_MCP_TOOLS = {
    "fetch_latest_verified_tee_model_bundle",
    "generate_random_tee_chestmnist_image",
    "run_and_verify_tee_inference",
    "fetch_latest_verified_zk_model_bundle",
    "generate_random_zk_chestmnist_image",
    "generate_and_verify_zk_inference_proof",
}


class AgentDashboardTests(unittest.TestCase):
    def test_dashboard_has_one_panel_for_each_public_mcp_tool(self) -> None:
        root = Path(__file__).resolve().parents[2]
        dashboard = json.loads(
            (root / "observability/grafana/dashboards/agent-mcp-tool-usage.json").read_text(
                encoding="utf-8"
            )
        )
        queries = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
        tool_names = {
            match.group(1)
            for query in queries
            if (match := re.search(r'tool_name="([^"]+)"', query)) is not None
        }

        self.assertEqual(tool_names, EXPECTED_MCP_TOOLS)
        self.assertEqual(len(dashboard["panels"]), len(EXPECTED_MCP_TOOLS))


if __name__ == "__main__":
    unittest.main()
