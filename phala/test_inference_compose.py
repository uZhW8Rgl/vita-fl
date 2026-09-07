import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


class InferenceComposeTemplateTests(unittest.TestCase):
    def test_zk_inference_receives_the_same_measured_chain_policy_as_tee_inference(self) -> None:
        template = (ROOT / "dstack-compose.zk-inference.phala.tftpl").read_text()
        expected_entries = (
            'RPC_URL: "${rpc_url}"',
            'EXPECTED_RUNTIME_RPC_URL: "${rpc_url}"',
            'EXPECTED_GM_STORAGE_ADDRESS: "${expected_gm_storage_address}"',
            'EXPECTED_DEVICE_REGISTRY_ADDRESS: "${expected_device_registry_address}"',
            'EXPECTED_CHAIN_ID: "${expected_chain_id}"',
        )

        for entry in expected_entries:
            with self.subTest(entry=entry):
                self.assertIn(entry, template)

    def test_terraform_supplies_every_zk_inference_policy_template_value(self) -> None:
        terraform = (ROOT / "main.tf").read_text()
        expected_assignments = (
            "expected_gm_storage_address      = var.expected_gm_storage_address",
            "expected_device_registry_address = var.expected_device_registry_address",
            "expected_chain_id                = var.expected_chain_id",
        )

        for assignment in expected_assignments:
            with self.subTest(assignment=assignment):
                self.assertIn(assignment, terraform)


if __name__ == "__main__":
    unittest.main()
