"""Exercise reset recovery with Terraform's built-in provider, without a cloud."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from phala.resolve_images import IMAGE_KEYS

DIRECTORY = Path(__file__).resolve().parent
TERRAFORM = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
if not TERRAFORM and (DIRECTORY / "bin/terraform").is_file():
    TERRAFORM = str(DIRECTORY / "bin/terraform")


@unittest.skipUnless(TERRAFORM, "Terraform is needed for the isolated built-in-provider recovery test")
class ImageManifestRecoveryTests(unittest.TestCase):
    def test_selected_images_and_revisions_survive_destroy_and_failed_recreation(self):
        with tempfile.TemporaryDirectory(prefix="phala-manifest-test-") as temporary:
            directory = Path(temporary)
            # Test the actual manifest and output definitions. The simulated
            # application uses the built-in provider, so no credentials or
            # Phala/network access are involved.
            (directory / "image-manifest.tf").write_text((DIRECTORY / "image-manifest.tf").read_text())
            output_source = (DIRECTORY / "outputs.tf").read_text()
            (directory / "outputs.tf").write_text(output_source[output_source.index('output "deployment_images" {') :])
            variables = "\n".join(f'variable "{key}" {{ type = string }}' for key in IMAGE_KEYS)
            (directory / "main.tf").write_text(
                variables
                + '\nvariable "deployment_image_revisions" { type = map(string) }\n'
                + 'variable "can_create_app" { default = true }\n'
                + """
resource "terraform_data" "simulated_app" {
  input = "application"
  lifecycle {
    precondition {
      condition     = var.can_create_app
      error_message = "Simulated Phala recreation failure."
    }
  }
}
"""
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("TF_VAR_", "TF_CLI_ARGS")) and key not in {"TF_WORKSPACE", "TF_DATA_DIR"}
            }
            environment["TF_DATA_DIR"] = str(directory / ".terraform")

            def terraform(*args, expected=0):
                result = subprocess.run(
                    [TERRAFORM, f"-chdir={directory}", *args],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout

            initial_images = {key: f"ghcr.io/example/{key}@sha256:" + "a" * 64 for key in IMAGE_KEYS}
            initial_revisions = {key: "b" * 40 for key in IMAGE_KEYS}
            selected = {**initial_images, "deployment_image_revisions": initial_revisions}
            variables_file = directory / "test.auto.tfvars.json"
            variables_file.write_text(json.dumps(selected))
            terraform("init", "-input=false", "-no-color")
            terraform("apply", "-input=false", "-auto-approve", "-no-color")

            selected["worker_image"] = "ghcr.io/example/worker@sha256:" + "c" * 64
            selected["deployment_image_revisions"] = {**initial_revisions, "worker_image": "d" * 40}
            variables_file.write_text(json.dumps(selected))
            terraform(
                "apply", "-input=false", "-auto-approve", "-no-color", "-target=terraform_data.deployment_manifest"
            )
            terraform("destroy", "-input=false", "-auto-approve", "-no-color", "-target=terraform_data.simulated_app")
            terraform("apply", "-input=false", "-auto-approve", "-no-color", "-var=can_create_app=false", expected=1)

            recovered = json.loads(terraform("output", "-json"))
            self.assertEqual(recovered["deployment_images"]["value"], {key: selected[key] for key in IMAGE_KEYS})
            self.assertEqual(recovered["deployment_image_revisions"]["value"], selected["deployment_image_revisions"])
            self.assertEqual(terraform("state", "list").strip(), "terraform_data.deployment_manifest")


if __name__ == "__main__":
    unittest.main()
