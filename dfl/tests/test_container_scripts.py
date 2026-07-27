from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "dfl" / "start_node_neural_network.sh"
HEALTHCHECK = ROOT / "dfl" / "container_healthcheck.sh"
NODE_SERVER_SOURCE = ROOT / "dfl" / "node_server" / "src" / "server.ts"
NODE_SERVER_DIST = ROOT / "dfl" / "node_server" / "dist" / "server.js"


class CombinedWorkerScriptTests(unittest.TestCase):
    def test_start_script_is_valid_bash(self) -> None:
        subprocess.run(["bash", "-n", str(START_SCRIPT)], check=True)

    def test_shell_bootstrap_waits_for_admission_but_not_final_readiness(self) -> None:
        script = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"/runtime/admission-ready.json"', script)
        self.assertIn('"status") == "admission-ready"', script)
        self.assertIn('"/runtime/contracts.json"', script)
        self.assertNotIn('"/runtime/ready.json"', script)

    def test_local_file_provider_preserves_the_configured_fixture_key_source(self) -> None:
        script = START_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'PARTICIPANT_RSA_PRIVATE_KEY_FILE:-${RSA_PRIVATE_KEY_FILE:-}',
            script,
        )

    def test_completed_training_releases_the_training_python_service(self) -> None:
        script = START_SCRIPT.read_text(encoding="utf-8")
        completion = script.index(
            'echo "DFL training process completed; stopping the training-only Python service."'
        )
        inference_wait = script.index('wait "${TEE_INFERENCE_PID}"', completion)
        self.assertIn('kill "${PYTHON_PID}"', script[completion:inference_wait])
        self.assertIn('PYTHON_PID=""', script[completion:inference_wait])

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
