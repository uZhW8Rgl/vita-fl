"""Render the shipped agent ZK configuration without cloud access."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
TERRAFORM = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
if not TERRAFORM and (DIRECTORY / "bin/terraform").is_file():
    TERRAFORM = str(DIRECTORY / "bin/terraform")


@unittest.skipUnless(TERRAFORM, "Terraform is needed for the offline agent configuration test")
class AgentZkConfigurationTests(unittest.TestCase):
    def test_disabled_zk_does_not_inherit_a_previous_deployments_endpoint(self):
        main = (DIRECTORY / "main.tf").read_text()
        template = (DIRECTORY / "dstack-compose.contracts.phala.tftpl").read_text()
        enabled = re.search(r"^\s*enable_zk_inference\s*=\s*(.+)$", main, re.MULTILINE)
        endpoint = re.search(r'^\s*zk_inference_url\s*=.*?\)\s*:\s*""', main, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(enabled)
        self.assertIsNotNone(endpoint)
        lines = [line.strip() for line in template.splitlines() if re.match(r"\s*ZK_INFERENCE_(URL|ENABLED):", line)]
        with tempfile.TemporaryDirectory(prefix="phala-agent-zk-test-") as temporary:
            directory = Path(temporary)
            (directory / "agent.tftpl").write_text("\n".join(lines))
            (directory / "main.tf").write_text(
                'variable "enable_zk_inference" { type = bool }\n'
                'variable "zk_inference_url_override" { type = string }\n'
                'output "agent_environment" {\n'
                '  value = templatefile("${path.module}/agent.tftpl", {\n'
                f"    enable_zk_inference = {enabled.group(1)}\n"
                f"    {endpoint.group().strip()}\n"
                "  })\n}\n"
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("TF_VAR_", "TF_CLI_ARGS")) and key not in {"TF_DATA_DIR", "TF_WORKSPACE"}
            }
            environment["TF_DATA_DIR"] = str(directory / ".terraform")

            def terraform(*args):
                result = subprocess.run(
                    [TERRAFORM, f"-chdir={directory}", *args], env=environment, text=True, capture_output=True
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            terraform("init", "-input=false", "-no-color")
            for url in (None, "https://deleted-cvm.example.test/"):
                with self.subTest(url=url):
                    (directory / "test.auto.tfvars.json").write_text(
                        json.dumps(
                            {
                                "enable_zk_inference": False,
                                "zk_inference_url_override": url,
                            }
                        )
                    )
                    terraform("apply", "-input=false", "-auto-approve", "-no-color")
                    rendered = json.loads(terraform("output", "-json"))["agent_environment"]["value"]
                    self.assertEqual(rendered, 'ZK_INFERENCE_URL: ""\nZK_INFERENCE_ENABLED: "false"')


if __name__ == "__main__":
    unittest.main()
