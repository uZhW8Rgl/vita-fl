"""Exercise Terraform argument/endpoint precedence without any cloud access."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class TerraformEnvironmentTests(unittest.TestCase):
    def invoke(
        self, *arguments, include_api_key=True, shared_unreadable=False, use_default=False, check=True,
        explicit_os=None,
    ):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script_directory = directory / "phala"
            script_directory.mkdir()
            for filename in ("tf-env.sh", "build_worker_inventory.py"):
                shutil.copyfile(root / filename, script_directory / filename)
            # Run from an isolated checkout: never consult the developer's private files.
            shared_env = directory / ".env.shared"
            shared_env.write_text(
                "PHALA_CLOUD_API_KEY=wrong-shared-key\n"
                "ETH_WALLET_PRIVATE_KEY=wrong-shared-wallet\n"
                "WORKER_IMAGE=wrong-shared-image\n"
            )
            if shared_unreadable:
                shared_env.chmod(0)
            env_file = directory / (".env.phala.anvil" if use_default else "deployment.env")
            env_file.write_text(
                "\n".join(
                    [
                        "PHALA_CLOUD_API_KEY=test" if include_api_key else "# no API key",
                        "W0_ACCOUNT_ADDRESS=0x" + "1" * 40,
                        "W0_PRIVATE_KEY=0x" + "2" * 64,
                        "W0_DEVICE_ID=0",
                        "W0_RSA_PRIVATE_KEY=unused",
                        "W0_RSA_PUBLIC_KEY=unused",
                        "MAX_DYNAMIC_WORKERS=1",
                        "WORKER_COUNT=1",
                        "ENABLE_PHALA_CONTROL_API=false",
                        "ENABLE_PHALA_UI=false",
                        "ENABLE_PHALA_AGENT=false",
                        "ENABLE_OLLAMA=false",
                        "ENABLE_SELLO_RECEIPTS=false",
                        "PHALA_RUNTIME_ENDPOINT_OVERRIDE=https://old-5001.example",
                        "PHALA_RUNTIME_RPC_URL=https://old-8545.example",
                        "WORKER_IMAGE=stale",
                        "PHALA_OS_IMAGE=selected-app-os",
                        "PHALA_CONTRACTS_OS_IMAGE=selected-runtime-os",
                        "PHALA_DYNAMIC_WORKER_OS_IMAGE=dstack-dev-0.5.9",
                    ]
                )
            )
            fake_terraform = directory / "terraform"
            fake_terraform.write_text(
                "#!/usr/bin/env python3\nimport json, os, sys\n"
                "print(json.dumps({'args': sys.argv[1:], 'rpc': os.environ.get('TF_VAR_runtime_rpc_url_override'), "
                "'worker': os.environ.get('TF_VAR_worker_image'), "
                "'api_key': os.environ.get('TF_VAR_phala_cloud_api_key'), "
                "'os': os.environ.get('TF_VAR_os_image'), "
                "'runtime_os': os.environ.get('TF_VAR_contracts_os_image'), "
                "'worker_os': os.environ.get('TF_VAR_dynamic_worker_os_image'), "
                "'wallet': os.environ.get('TF_VAR_eth_wallet_private_key')}))\n"
            )
            fake_terraform.chmod(0o700)
            images = directory / "images.tfvars.json"
            images.write_text("{}")
            env = {key: value for key, value in os.environ.items() if not key.startswith(("TF_", "PHALA_"))}
            env.update(
                TERRAFORM_BIN=str(fake_terraform),
                PHALA_IMAGE_VARS_FILE=str(images),
                TF_VAR_runtime_rpc_url_override="https://new-8545.example",
                TF_VAR_worker_image="explicit",
            )
            if not use_default:
                env["PHALA_ENV_FILE"] = str(env_file)
            if explicit_os is not None:
                env["TF_VAR_os_image"] = explicit_os
            result = subprocess.run(
                ["bash", str(script_directory / "tf-env.sh"), *arguments],
                env=env,
                capture_output=True,
                text=True,
                check=check,
            )
            if check:
                self.assertEqual(result.stderr, "")
                return json.loads(result.stdout), str(images)
            return result, str(images)

    def test_single_selected_file_ignores_shared_values_and_uses_worker_wallet_fallback(self):
        for use_default in (False, True):
            with self.subTest(use_default=use_default):
                actual, _ = self.invoke("plan", use_default=use_default)
                self.assertEqual(actual["api_key"], "test")
                self.assertEqual(actual["wallet"], "0x" + "2" * 64)

    def test_shared_file_cannot_supply_missing_api_key(self):
        result, _ = self.invoke("plan", include_api_key=False, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PHALA_CLOUD_API_KEY is required in", result.stderr)
        self.assertNotIn(".env.shared", result.stderr)
        self.assertNotIn("wrong-shared-key", result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_app_os_override_preserves_explicit_tf_precedence_and_worker_policy(self):
        for explicit in (None, "explicit-app-os"):
            with self.subTest(explicit=explicit):
                actual, _ = self.invoke("plan", explicit_os=explicit)
                self.assertEqual(actual["os"], explicit or "selected-app-os")
                self.assertEqual(actual["runtime_os"], "selected-runtime-os")
                self.assertEqual(actual["worker_os"], "dstack-dev-0.5.9")

    def test_unreadable_shared_file_does_not_affect_terraform(self):
        actual, _ = self.invoke("plan", shared_unreadable=True)
        self.assertEqual(actual["api_key"], "test")

    def test_launcher_endpoints_override_old_env_and_image_file_overrides_tfvars(self):
        actual, image_file = self.invoke("plan", "-input=false", "-var-file=old.tfvars")
        self.assertEqual(actual["rpc"], "https://new-8545.example")
        self.assertEqual(actual["worker"], "explicit")
        self.assertEqual(actual["args"][-1], "-var-file=" + image_file)

    def test_import_flags_come_before_resource_and_id(self):
        actual, image_file = self.invoke("import", "-input=false", "phala_app.contract_runtime", "test-id")
        self.assertEqual(actual["args"][-3:], ["-var-file=" + image_file, "phala_app.contract_runtime", "test-id"])

    def test_separate_option_value_keeps_resolved_images_last(self):
        actual, image_file = self.invoke("apply", "-var-file", "old.tfvars")
        self.assertEqual(actual["args"][-3:], ["-var-file", "old.tfvars", "-var-file=" + image_file])

    def test_saved_plan_apply_does_not_add_variables(self):
        actual, image_file = self.invoke("apply", "-input=false", "saved-plan")
        self.assertEqual(actual["args"][-1], "saved-plan")
        self.assertNotIn("-var-file=" + image_file, actual["args"])


if __name__ == "__main__":
    unittest.main()
