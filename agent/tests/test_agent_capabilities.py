from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent import agent_skills, mcp_server


class AgentCapabilitiesTests(unittest.TestCase):
    def test_disabled_zk_is_not_advertised_to_the_planner(self):
        with patch.dict(os.environ, {"ZK_INFERENCE_ENABLED": "false"}):
            prompt = agent_skills.describe_agent_skills()
        self.assertIn("fetch_latest_verified_tee_model_bundle", prompt)
        self.assertNotIn("fetch_latest_verified_zk_model_bundle", prompt)
        self.assertNotIn("generate_and_verify_zk_inference_proof", prompt)

    def test_local_deployments_can_keep_zk_enabled(self):
        with patch.dict(os.environ, {"ZK_INFERENCE_ENABLED": "true"}):
            self.assertIn("fetch_latest_verified_zk_model_bundle", agent_skills.describe_agent_skills())

    def test_disabled_zk_never_contacts_a_stale_endpoint(self):
        with (
            patch.dict(os.environ, {"ZK_INFERENCE_ENABLED": "false"}),
            patch.object(mcp_server, "ZK_INFERENCE_URL", "https://deleted-cvm.example.test"),
            patch("urllib.request.urlopen") as request,
        ):
            for operation in (
                mcp_server.fetch_latest_verified_zk_model_bundle,
                lambda: mcp_server._remote_get_bytes("/v1/bundles/example"),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(RuntimeError, "ZK inference is disabled"):
                        operation()
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
