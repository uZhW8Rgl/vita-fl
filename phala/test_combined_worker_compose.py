from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parent


class CombinedWorkerComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.static_template = (
            ROOT / "dstack-compose.worker.phala.tftpl"
        ).read_text(encoding="utf-8")
        self.dynamic_template = (
            ROOT / "dynamic-workers/worker-compose.tftpl"
        ).read_text(encoding="utf-8")

    def test_worker_templates_keep_sealed_state_and_plaintext_keys_separate(self) -> None:
        required = (
            "- /var/run/dstack.sock:/var/run/dstack.sock",
            "- participant-key-state:/var/lib/vita-fl",
            "- /run/vita-fl:size=16m,mode=0700",
            "PARTICIPANT_KEY_STATE_PATH: /var/lib/vita-fl/participant-rsa.v1.sealed.json",
            "PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH: /run/vita-fl/participant-private.pem",
            "PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH: /run/vita-fl/participant-public.pem",
            "volumes:\n  participant-key-state:",
        )
        for template in (self.static_template, self.dynamic_template):
            for entry in required:
                with self.subTest(template=template[:20], entry=entry):
                    self.assertIn(entry, template)

    def test_worker_templates_reboot_without_policy_split(self) -> None:
        for template in (self.static_template, self.dynamic_template):
            with self.subTest(template=template[:20]):
                self.assertIn("restart: unless-stopped", template)
                self.assertNotIn("restart: on-failure", template)

        compatibility_compose = (ROOT / "dstack-compose.template.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("restart: unless-stopped", compatibility_compose)
        self.assertNotIn("restart: on-failure", compatibility_compose)

    def test_dynamic_worker_os_image_uses_provider_canonical_slug(self) -> None:
        canonical = "dstack-dev-0.5.9"
        deprecated = canonical + "-de9c74f0"
        root_variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        dynamic_variables = (
            ROOT / "dynamic-workers/variables.tf"
        ).read_text(encoding="utf-8")
        environment_example = (
            REPOSITORY_ROOT / ".env.phala.anvil.example"
        ).read_text(encoding="utf-8")

        self.assertRegex(
            root_variables,
            rf'(?s)variable "dynamic_worker_os_image".*?default\s+= "{re.escape(canonical)}"',
        )
        self.assertRegex(
            dynamic_variables,
            rf'(?s)variable "os_image".*?default\s+= "{re.escape(canonical)}"',
        )
        self.assertIn(
            f"PHALA_DYNAMIC_WORKER_OS_IMAGE={canonical}",
            environment_example,
        )
        self.assertNotIn(
            f"PHALA_DYNAMIC_WORKER_OS_IMAGE={deprecated}",
            environment_example,
        )

    def test_cost_scenario_metadata_reaches_every_worker_template(self) -> None:
        expected_entries = (
            'ETH_EUR_PRICE: "${eth_eur_price}"',
            'ETH_USD_PRICE: "${eth_usd_price}"',
            'EXCHANGE_RATE_SOURCE: "${exchange_rate_source}"',
            'EXCHANGE_RATE_TIMESTAMP_UTC: "${exchange_rate_timestamp_utc}"',
            'REFERENCE_MAINNET_GAS_PRICE_GWEI: "${reference_mainnet_gas_price_gwei}"',
            'REFERENCE_GAS_PRICE_SOURCE: "${reference_gas_price_source}"',
            'REFERENCE_GAS_PRICE_TIMESTAMP_UTC: "${reference_gas_price_timestamp_utc}"',
        )
        for template in (self.static_template, self.dynamic_template):
            for entry in expected_entries:
                with self.subTest(template=template[:20], entry=entry):
                    self.assertIn(entry, template)

    def test_training_optimization_inputs_reach_every_worker_path(self) -> None:
        expected_template_entries = (
            'DFL_MODEL_SEED: "${dfl_model_seed}"',
            'DFL_TRAIN_SEED: "${dfl_train_seed}"',
            'DFL_TRAIN_OPTIMIZER: "${dfl_train_optimizer}"',
            'DFL_TRAIN_LEARNING_RATE: "${dfl_train_learning_rate}"',
            'DFL_TRAIN_LR_SCHEDULE: "${dfl_train_lr_schedule}"',
            'DFL_TRAIN_LR_DECAY_START_ROUND: "${dfl_train_lr_decay_start_round}"',
            'DFL_TRAIN_LR_FINAL_FACTOR: "${dfl_train_lr_final_factor}"',
            'DFL_TRAIN_WEIGHT_DECAY: "${dfl_train_weight_decay}"',
            'DFL_GRAD_CLIP_NORM: "${dfl_grad_clip_norm}"',
            'DFL_POS_WEIGHT_CAP: "${dfl_pos_weight_cap}"',
        )
        for template in (self.static_template, self.dynamic_template):
            for entry in expected_template_entries:
                with self.subTest(template=template[:20], entry=entry):
                    self.assertIn(entry, template)

        manual_template = (ROOT / "dstack-compose.template.yml").read_text(
            encoding="utf-8"
        )
        for environment_name in (
            "DFL_MODEL_SEED",
            "DFL_TRAIN_SEED",
            "DFL_TRAIN_OPTIMIZER",
            "DFL_TRAIN_LEARNING_RATE",
            "DFL_TRAIN_LR_SCHEDULE",
            "DFL_TRAIN_LR_DECAY_START_ROUND",
            "DFL_TRAIN_LR_FINAL_FACTOR",
            "DFL_TRAIN_WEIGHT_DECAY",
            "DFL_GRAD_CLIP_NORM",
            "DFL_POS_WEIGHT_CAP",
        ):
            with self.subTest(manual_environment_name=environment_name):
                self.assertIn(f"      {environment_name}:", manual_template)

        contracts_template = (
            ROOT / "dstack-compose.contracts.phala.tftpl"
        ).read_text(encoding="utf-8")
        for environment_name in (
            "DFL_MODEL_SEED",
            "DFL_TRAIN_SEED",
            "DFL_TRAIN_OPTIMIZER",
            "DFL_TRAIN_LEARNING_RATE",
            "DFL_TRAIN_LR_SCHEDULE",
            "DFL_TRAIN_LR_DECAY_START_ROUND",
            "DFL_TRAIN_LR_FINAL_FACTOR",
            "DFL_TRAIN_WEIGHT_DECAY",
            "DFL_GRAD_CLIP_NORM",
            "DFL_POS_WEIGHT_CAP",
        ):
            with self.subTest(environment_name=environment_name):
                self.assertIn(f"      {environment_name}:", contracts_template)

    def test_completed_training_reboots_once_then_keeps_worker_idle(self) -> None:
        supervisor = (
            REPOSITORY_ROOT / "dfl/start_node_neural_network.sh"
        ).read_text(encoding="utf-8")
        worker = (
            REPOSITORY_ROOT / "dfl/node_server/src/server.ts"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "exiting once so Docker reboots the combined worker",
            supervisor,
        )
        self.assertNotIn("Keeping only the TEE inference receiver available", supervisor)
        self.assertIn("trainingWasCompleteAtStartup", worker)
        self.assertIn("training.rebooted_idle", worker)

    def test_only_inference_worker_exposes_receiver_state_and_port(self) -> None:
        conditional_receiver = re.compile(
            r"%\{ if inference_enabled ~\}"
            r"(?P<body>.*?)"
            r"%\{ endif ~\}",
            re.DOTALL,
        )
        for template in (self.static_template, self.dynamic_template):
            conditional_bodies = [
                match.group("body")
                for match in conditional_receiver.finditer(template)
            ]
            self.assertTrue(
                any("/tmp/tee-inference:size=256m,mode=0700" in body for body in conditional_bodies)
            )
            self.assertTrue(any('"8080:8080"' in body for body in conditional_bodies))
            self.assertTrue(
                any('SELLO_SERVICE_KEY_PROVIDER: "dstack"' in body for body in conditional_bodies)
            )
            self.assertNotIn("SELLO_SERVICE_SIGNING_SEED", template)
            self.assertIn('- "8001:8001"', template)
            self.assertIn(
                'TEE_INFERENCE_ENABLED: "${inference_enabled ? 1 : 0}"',
                template,
            )

    def test_aggregation_threshold_and_deadline_are_not_worker_security_inputs(self) -> None:
        manual_template = (ROOT / "dstack-compose.template.yml").read_text(
            encoding="utf-8"
        )
        for template in (
            self.static_template,
            self.dynamic_template,
            manual_template,
        ):
            with self.subTest(template=template[:20]):
                self.assertNotIn("CLIENT_LIMIT:", template)
                self.assertNotIn("MODEL_SUBMISSION_DEADLINE_MS:", template)

        dynamic_variables = (
            ROOT / "dynamic-workers/variables.tf"
        ).read_text(encoding="utf-8")
        dynamic_module = (
            ROOT / "dynamic-workers/main.tf"
        ).read_text(encoding="utf-8")
        self.assertNotIn('variable "client_limit"', dynamic_variables)
        self.assertNotIn(
            'variable "model_submission_deadline_ms"',
            dynamic_variables,
        )
        self.assertNotIn("client_limit", dynamic_module)
        self.assertNotIn("model_submission_deadline_ms", dynamic_module)

    def test_control_api_keeps_owner_policy_inputs_on_the_internal_runtime(self) -> None:
        contracts_template = (
            ROOT / "dstack-compose.contracts.phala.tftpl"
        ).read_text(encoding="utf-8")
        control_block = re.search(
            r"%\{ if enable_control_api ~\}(?P<body>.*?)%\{ endif ~\}",
            contracts_template,
            re.DOTALL,
        )

        self.assertIsNotNone(control_block)
        body = control_block.group("body")
        expected_entries = (
            'ETH_WALLET_PRIVATE_KEY: "$${ETH_WALLET_PRIVATE_KEY}"',
            "RPC_URL: http://anvil:8545",
            "KUBO_API_URL: http://ipfs:5001",
            'CLIENT_LIMIT: "${client_limit}"',
            'MODEL_SUBMISSION_DEADLINE_MS: "${model_submission_deadline_ms}"',
        )
        for entry in expected_entries:
            with self.subTest(entry=entry):
                self.assertIn(entry, body)

        self.assertEqual(contracts_template.count('CLIENT_LIMIT: "${client_limit}"'), 2)
        self.assertEqual(
            contracts_template.count(
                'MODEL_SUBMISSION_DEADLINE_MS: "${model_submission_deadline_ms}"'
            ),
            2,
        )

    def test_terraform_assigns_inference_role_only_to_worker_zero(self) -> None:
        root_module = (ROOT / "main.tf").read_text(encoding="utf-8")
        dynamic_module = (
            ROOT / "dynamic-workers/main.tf"
        ).read_text(encoding="utf-8")

        self.assertRegex(
            root_module,
            r'(?s)resource "phala_app" "dfl_worker".*?'
            r"inference_enabled\s+= true",
        )
        self.assertRegex(
            root_module,
            r'(?s)resource "phala_app" "dfl_worker_additional".*?'
            r"inference_enabled\s+= false",
        )
        self.assertIn(
            "inference_enabled                        = "
            "var.workers[each.key].device_id == 0",
            dynamic_module,
        )
        self.assertRegex(
            dynamic_module,
            r"var\.workers\[each\.key\]\.device_id == 0 \? "
            r"\{\s*SELLO_TOKEN_ISSUER_PUBLIC_KEY",
        )

    def test_policy_reference_and_live_workers_share_exact_runtime_rpc_url(self) -> None:
        root_module = (ROOT / "main.tf").read_text(encoding="utf-8")

        self.assertNotIn("runtime-policy-reference.invalid", root_module)
        self.assertRegex(
            root_module,
            r"worker_policy_runtime_rpc_url\s*=\s*coalesce\(\s*"
            r"var\.runtime_rpc_url_override,\s*"
            r'"http://runtime-endpoint-not-configured\.invalid",\s*\)',
        )
        self.assertRegex(
            root_module,
            r"worker_policy_reference_inputs\s*=\s*\{[\s\S]*?"
            r"rpc_url\s*=\s*local\.worker_policy_runtime_rpc_url",
        )
        self.assertRegex(
            root_module,
            r"contracts_rpc_url\s*=\s*coalesce\(\s*"
            r"var\.runtime_rpc_url_override,",
        )
        self.assertIn(
            "dynamic_worker_rpc_url               = "
            'var.runtime_rpc_url_override == null ? "" : var.runtime_rpc_url_override',
            root_module,
        )

        for template in (self.static_template, self.dynamic_template):
            with self.subTest(template=template[:20]):
                self.assertIn('RPC_URL: "${rpc_url}"', template)
                self.assertIn(
                    'EXPECTED_RUNTIME_RPC_URL: "${rpc_url}"',
                    template,
                )

    def test_no_separate_tee_inference_terraform_resource_remains(self) -> None:
        terraform_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "main.tf",
                ROOT / "outputs.tf",
                ROOT / "dynamic-workers/main.tf",
            )
        )
        self.assertNotRegex(
            terraform_sources,
            r'resource "phala_(?:app|cvm_power)" "tee_inference"',
        )
        self.assertFalse(
            (ROOT / "dstack-compose.tee-inference.phala.tftpl").exists()
        )

    def test_compatibility_output_targets_the_inference_port(self) -> None:
        outputs = (ROOT / "outputs.tf").read_text(encoding="utf-8")
        match = re.search(
            r'output "tee_inference_endpoint" \{(?P<body>.*?)\n\}',
            outputs,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn('"-8080."', body)
        self.assertIn('}:8080"', body)

    def test_image_healthcheck_uses_inference_after_key_materialization(self) -> None:
        dockerfile = (
            REPOSITORY_ROOT / "dfl/Dockerfile"
        ).read_text(encoding="utf-8")
        healthcheck = (
            REPOSITORY_ROOT / "dfl/container_healthcheck.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('EXPOSE 8001 8080', dockerfile)
        self.assertIn('CMD ["/dfl/container_healthcheck.sh"]', dockerfile)
        self.assertIn(
            '${PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH:-/run/vita-fl/participant-private.pem}',
            healthcheck,
        )
        self.assertIn("http://127.0.0.1:8080/healthz", healthcheck)
        self.assertIn("http://127.0.0.1:8000/health", healthcheck)


if __name__ == "__main__":
    unittest.main()
