from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "dfl" / "start_node_neural_network.sh"
HEALTHCHECK = ROOT / "dfl" / "container_healthcheck.sh"
DOCKERFILE = ROOT / "dfl" / "Dockerfile"
NODE_SERVER_SOURCE = ROOT / "dfl" / "node_server" / "src" / "server.ts"
NODE_SERVER_DIST = ROOT / "dfl" / "node_server" / "dist" / "server.js"


class CombinedWorkerScriptTests(unittest.TestCase):
    def test_start_script_is_valid_bash(self) -> None:
        subprocess.run(["bash", "-n", str(START_SCRIPT)], check=True)

    def test_shell_bootstrap_treats_mfs_as_optional_handoff(self) -> None:
        script = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"/runtime/admission-ready.json"', script)
        self.assertIn('"status") == "admission-ready"', script)
        self.assertIn('"/runtime/contracts.json"', script)
        self.assertNotIn('"/runtime/ready.json"', script)
        self.assertIn("read_optional_mfs_json", script)
        self.assertIn(
            "The Compose-measured RPC endpoint and EXPECTED_* addresses are the durable",
            script,
        )
        self.assertNotIn("manifest_deadline", script)
        self.assertNotIn("Waiting for contract runtime admission marker", script)
        self.assertIn(
            "registry_address from runtime admission marker does not match measured",
            script,
        )

        server = NODE_SERVER_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("/runtime/ready.json", server)
        self.assertIn("/runtime/bootstrap-recipients.json", server)
        self.assertIn("waitForRegisteredBootstrapRecipients", server)

    def test_round_zero_encrypts_unsigned_initial_model_without_worker_training(self) -> None:
        server = NODE_SERVER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("prepareRoundZeroBootstrap", server)
        self.assertIn("Buffer.alloc(0)", server)
        self.assertIn("fixedRoundZeroRecipients", server)
        self.assertIn("waitForRoundZeroBootstrap", server)
        self.assertNotIn("prepareRoundZeroBootstrapRollover", server)

    def test_round_zero_bootstrap_survives_worker_restart_in_updating(self) -> None:
        server = NODE_SERVER_SOURCE.read_text(encoding="utf-8")
        ipfs = (ROOT / "dfl" / "node_server" / "src" / "ipfs.ts").read_text(
            encoding="utf-8"
        )
        snapshot = (
            ROOT / "dfl" / "node_server" / "src" / "bootstrap_snapshot.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("round-0-bootstrap-recipients.json", server)
        self.assertIn("PARTICIPANT_KEY_STATE_PATH", server)
        self.assertIn("public_key_der_hex", snapshot)
        self.assertIn("run_roster_digest", snapshot)
        self.assertNotIn("declaration_generation_sha256", snapshot)
        self.assertNotIn("admission_generation_sha256", snapshot)
        self.assertIn("getCommittedRunRosterState", server)
        self.assertIn("frozenRecipientsForCommittedRoster", server)
        self.assertIn("onchain_roster_digest", server)
        self.assertIn("getCommittedRunRosterState", ipfs)
        self.assertIn("frozenBootstrapRecipients", ipfs)
        self.assertIn("aggregator.round0.bootstrap_recovery", server)
        self.assertIn(
            "Recovering the round-0 bootstrap artifacts from the immutable initial-model CID.",
            server,
        )
        snapshot_reuse = server[
            server.index("const existing = JSON.parse(await fs.readFile(snapshotPath"):
            server.index("const frozen = await waitForRegisteredBootstrapRecipients()")
        ]
        self.assertNotIn("readRuntimeMfsJson", snapshot_reuse)
        self.assertNotIn("validateBootstrapDeclarationAgainstCommittedRoster", snapshot_reuse)
        self.assertIn("requireFrozenRecipientKeysMatchRegistry", snapshot_reuse)

    def test_updating_recovery_does_not_treat_aborted_round_as_finalized(self) -> None:
        client = (
            ROOT / "dfl" / "node_server" / "src" / "bc_client.ts"
        ).read_text(encoding="utf-8")
        server = NODE_SERVER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("roundCompleted(sourceRound)", client)
        self.assertIn("isGlobalModelPublished(finalization.sourceRound)", server)
        self.assertIn("isRoundCompleted(finalization.sourceRound)", server)
        self.assertIn("isExplicitlyFinalizedSourceRound", server)
        ipfs = (
            ROOT / "dfl" / "node_server" / "src" / "ipfs.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("isRoundCompleted(intendedSourceRound)", ipfs)
        self.assertIn("isRoundCompleted(sourceRound)", ipfs)

    def test_worker_binds_decrypted_model_to_atomic_finalized_bundle_round(self) -> None:
        client = (
            ROOT / "dfl" / "node_server" / "src" / "bc_client.ts"
        ).read_text(encoding="utf-8")
        crypto_source = (
            ROOT / "dfl" / "node_server" / "src" / "gm_crypto.ts"
        ).read_text(encoding="utf-8")
        server = NODE_SERVER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("getFinalizedModelBundle().call()", client)
        self.assertIn("modelRound", client)
        self.assertIn("expectedModelRound", crypto_source)
        self.assertIn("reconcileFetchedGlobalModel", server)

    def test_local_file_provider_preserves_the_configured_fixture_key_source(self) -> None:
        script = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'PARTICIPANT_RSA_PRIVATE_KEY_FILE:-${RSA_PRIVATE_KEY_FILE:-}',
            script,
        )

    def test_chestmnist_validation_artifact_is_packaged_and_copied_to_runtime(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        script = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "COPY data/chestmnist/validation_data ./config/chestmnist/validation_data",
            dockerfile,
        )
        self.assertIn(
            "CHESTMNIST_VALIDATION_DATA=/dfl/config/chestmnist/validation_data/validation-data.npz",
            dockerfile,
        )
        self.assertIn("VALIDATION_DATA_SRC=${VALIDATION_DATA_SRC:-${CHESTMNIST_VALIDATION_DATA:-", script)
        self.assertIn(
            'cp "${VALIDATION_DATA_SRC}" /dfl/node_server/data/validation-data.npz',
            script,
        )

    def test_completed_training_exits_once_and_cleanup_releases_services(self) -> None:
        script = START_SCRIPT.read_text(encoding="utf-8")
        completion = script.index(
            'echo "DFL training process completed; exiting once so Docker reboots the combined worker."'
        )
        completion_branch = script[completion:script.index("\nfi", completion)]
        self.assertIn("exit 0", completion_branch)
        self.assertNotIn('wait "${TEE_INFERENCE_PID}"', completion_branch)

        cleanup = script[script.index("cleanup() {"):script.index("terminate() {")]
        self.assertIn(
            'for pid in "${NODE_PID}" "${TEE_INFERENCE_PID}" "${PYTHON_PID}"',
            cleanup,
        )
        self.assertIn('kill "${pid}"', cleanup)
        self.assertIn('wait "${pid}"', cleanup)

    def test_combined_receiver_keeps_key_until_container_cleanup(self) -> None:
        start_script = START_SCRIPT.read_text(encoding="utf-8")
        cleanup = start_script[start_script.index("cleanup() {"):start_script.index("terminate() {")]
        self.assertIn(
            "${PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH:-/run/vita-fl/participant-private.pem}",
            cleanup,
        )
        self.assertIn(
            "${PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH:-/run/vita-fl/participant-public.pem}",
            cleanup,
        )

        for server_path in (NODE_SERVER_SOURCE, NODE_SERVER_DIST):
            with self.subTest(server=server_path):
                server = server_path.read_text(encoding="utf-8")
                self.assertIn(
                    "const retainParticipantPrivateKeyForInference = teeInferenceEnabled();",
                    server,
                )
                self.assertIn(
                    "if (retainParticipantPrivateKeyForInference)",
                    server,
                )
                self.assertIn(
                    "Retaining the runtime participant private key for the co-located TEE inference receiver.",
                    server,
                )
                self.assertIn(
                    "Leaving runtime participant-key cleanup to the combined-service supervisor.",
                    server,
                )

    def _healthcheck_url(self, enabled: str, *, participant_key_ready: bool = False) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            record = root / "curl-arguments"
            participant_key = root / "participant-private.pem"
            if participant_key_ready:
                participant_key.write_text("test-key", encoding="utf-8")
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"${HEALTHCHECK_TEST_RECORD}\"\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "HEALTHCHECK_TEST_RECORD": str(record),
                    "TEE_INFERENCE_ENABLED": enabled,
                    "PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH": str(participant_key),
                }
            )
            subprocess.run(["bash", str(HEALTHCHECK)], env=environment, check=True)
            return record.read_text(encoding="utf-8").splitlines()[-1]

    def test_healthcheck_uses_internal_ml_service_for_training_only_worker(self) -> None:
        self.assertEqual(
            self._healthcheck_url("0"),
            "http://127.0.0.1:8000/health",
        )

    def test_healthcheck_uses_public_inference_service_for_combined_worker(self) -> None:
        self.assertEqual(
            self._healthcheck_url("true", participant_key_ready=True),
            "http://127.0.0.1:8080/healthz",
        )

    def test_healthcheck_accepts_initializing_combined_worker(self) -> None:
        self.assertEqual(
            self._healthcheck_url("true"),
            "http://127.0.0.1:8000/health",
        )


if __name__ == "__main__":
    unittest.main()
