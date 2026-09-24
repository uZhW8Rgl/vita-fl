"""Render the shipped runtime endpoint expressions with offline Terraform."""

from __future__ import annotations

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

ENDPOINTS = {
    "rpc_url": "https://runtime-8545.example.test",
    "kubo_api_url": "https://runtime-5001.example.test",
    "kubo_gateway_url": "https://runtime-8080.example.test",
}


@unittest.skipUnless(TERRAFORM, "Terraform is needed for the offline runtime bootstrap regression")
class RuntimeBootstrapTests(unittest.TestCase):
    def test_static_and_dynamic_worker_security_topology_render_identically(self):
        templates = {
            "static": (DIRECTORY / "dstack-compose.worker.phala.tftpl").read_text(),
            "dynamic": (DIRECTORY / "dynamic-workers/worker-compose.tftpl").read_text(),
        }
        names = set()
        for template in templates.values():
            names.update(re.findall(r"(?<!\$)\$\{([A-Za-z_]\w*)\}", template))
        values = {name: f"test-{name}" for name in names}
        values.update(
            {
                "pki_ca_url": "https://ca.example.test:9443",
                "pki_root_fingerprint": "ab" * 32,
                "pki_worker_dns_name": "worker.example.test",
                "worker_image": "ghcr.io/example/worker@sha256:" + "cd" * 32,
                "telemetry_url": "",
                "agent_pop_registry": json.dumps({"master-thesis-agent": "test-agent-pop-public-key"}),
            }
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("TF_VAR_", "TF_CLI_ARGS")) and key not in {"TF_WORKSPACE", "TF_DATA_DIR"}
        }
        with tempfile.TemporaryDirectory(prefix="phala-worker-security-render-") as temporary:
            directory = Path(temporary)
            environment["TF_DATA_DIR"] = str(directory / ".terraform")
            for name, template in templates.items():
                (directory / f"{name}.tftpl").write_text(template)
            (directory / "main.tf").write_text(
                'variable "inference_enabled" { type = bool }\n'
                'locals { values = jsondecode(file("${path.module}/values.json")) }\n'
                + "\n".join(
                    f'output "{name}" {{ value = templatefile("${{path.module}}/{name}.tftpl", '
                    "merge(local.values, { inference_enabled = var.inference_enabled })) }"
                    for name in templates
                )
            )
            (directory / "values.json").write_text(json.dumps(values))

            def terraform(*arguments):
                result = subprocess.run(
                    [TERRAFORM, f"-chdir={directory}", *arguments],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            terraform("init", "-input=false", "-no-color")
            for inference_enabled in (False, True):
                with self.subTest(inference_enabled=inference_enabled):
                    terraform(
                        "apply",
                        "-input=false",
                        "-auto-approve",
                        "-no-color",
                        f"-var=inference_enabled={str(inference_enabled).lower()}",
                    )
                    rendered = json.loads(terraform("output", "-json"))
                    normalized = {}
                    for name in templates:
                        compose = rendered[name]["value"]
                        normalized[name] = [
                            line.strip()
                            for line in compose.splitlines()
                            if line.strip() and not line.lstrip().startswith("#")
                        ]
                        self.assertEqual('"8443:8443"' in compose, inference_enabled)
                        self.assertEqual('PKI_CA_URL: "https://ca.example.test:9443"' in compose, inference_enabled)
                        self.assertEqual(
                            'TEE_INFERENCE_ORIGIN: "https://worker.example.test"' in compose, inference_enabled
                        )
                        self.assertEqual('TEE_TRANSPORT_MODE: "ratls"' in compose, inference_enabled)
                        registry_line = "AGENT_POP_REGISTRY: " + json.dumps(values["agent_pop_registry"])
                        self.assertEqual(registry_line in compose, inference_enabled)
                        self.assertNotIn("PKI_ENROLLMENT_TOKEN:", compose)
                        self.assertNotIn("AGENT_POP_SIGNING_SEED", compose)
                        self.assertNotIn("SELLO_REQUIRED", compose)
                        self.assertNotIn('"8080:8080"', compose)
                        self.assertEqual(sum(line.lstrip().startswith("image:") for line in compose.splitlines()), 1)
                    self.assertEqual(normalized["static"], normalized["dynamic"])

    def test_runtime_compose_accepts_missing_and_explicit_endpoints(self):
        main = (DIRECTORY / "main.tf").read_text()
        variables = (DIRECTORY / "variables.tf").read_text()
        template = (DIRECTORY / "dstack-compose.contracts.phala.tftpl").read_text()
        assignments = []
        declarations = []
        template_lines = []
        for endpoint in ENDPOINTS:
            # Evaluate the real template arguments and their real variable
            # defaults. Extract only these endpoint fields so Terraform needs
            # no Phala provider, credentials, network, or existing state.
            assignment = re.search(rf"^\s*dynamic_worker_{endpoint}\s*=\s*(.+)$", main, re.MULTILINE)
            self.assertIsNotNone(assignment, endpoint)
            assignments.append(f"dynamic_worker_{endpoint} = {assignment.group(1)}")
            declaration = re.search(
                rf'^variable "runtime_{endpoint}_override" \{{.*?^\}}', variables, re.MULTILINE | re.DOTALL
            )
            self.assertIsNotNone(declaration, endpoint)
            declarations.append(declaration.group())
            template_line = re.search(rf"^\s*DYNAMIC_WORKER_{endpoint.upper()}:.*$", template, re.MULTILINE)
            self.assertIsNotNone(template_line, endpoint)
            template_lines.append(template_line.group().strip())

        with tempfile.TemporaryDirectory(prefix="phala-runtime-bootstrap-test-") as temporary:
            directory = Path(temporary)
            (directory / "runtime-endpoints.tftpl").write_text("\n".join(template_lines) + "\n")
            (directory / "main.tf").write_text(
                "\n".join(declarations)
                + '\noutput "runtime_environment" {\n'
                + '  value = templatefile("${path.module}/runtime-endpoints.tftpl", {\n'
                + "\n".join(assignments)
                + "\n  })\n}\n"
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("TF_VAR_", "TF_CLI_ARGS")) and key not in {"TF_WORKSPACE", "TF_DATA_DIR"}
            }
            environment["TF_DATA_DIR"] = str(directory / ".terraform")

            def terraform(*arguments):
                result = subprocess.run(
                    [TERRAFORM, f"-chdir={directory}", *arguments],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            terraform("init", "-input=false", "-no-color")
            cases = {
                "first launch without overrides": {},
                "bootstrap with explicit nulls": dict.fromkeys(ENDPOINTS),
                "explicit empty overrides": dict.fromkeys(ENDPOINTS, ""),
                "runtime endpoint wiring": ENDPOINTS,
            }
            for name, values in cases.items():
                with self.subTest(name=name):
                    (directory / "endpoints.auto.tfvars.json").write_text(
                        json.dumps({f"runtime_{endpoint}_override": value for endpoint, value in values.items()})
                    )
                    terraform("apply", "-input=false", "-auto-approve", "-no-color")
                    rendered = json.loads(terraform("output", "-json"))["runtime_environment"]["value"]
                    expected = (
                        "\n".join(
                            f'DYNAMIC_WORKER_{endpoint.upper()}: "{values.get(endpoint) or ""}"'
                            for endpoint in ENDPOINTS
                        )
                        + "\n"
                    )
                    self.assertEqual(rendered, expected)


if __name__ == "__main__":
    unittest.main()
