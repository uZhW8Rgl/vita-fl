"""Run the combined supervisor with stub services to check origin propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "dfl/start_node_neural_network.sh"
ORIGIN = "https://" + "a1" * 20 + "-8443s.dstack-prod.phala.network"

STUB = """import json, os, sys, time
from pathlib import Path
root = Path(os.environ["STARTUP_TEST_ROOT"])
name = Path(sys.argv[0]).name
if name == "curl":
    raise SystemExit(0)
if name == "node":
    (root / "node.json").write_text(json.dumps({"TEE_INFERENCE_ORIGIN": os.environ.get("TEE_INFERENCE_ORIGIN")}))
    Path(os.environ["RSA_PRIVATE_KEY_FILE"]).write_text("fixture-private")
    Path(os.environ["RSA_PUBLIC_KEY_FILE"]).write_text("fixture-public")
    deadline = time.monotonic() + 5
    while not (root / "receiver.json").exists():
        if time.monotonic() > deadline:
            raise SystemExit(1)
        time.sleep(0.01)
    time.sleep(0.5)
    raise SystemExit(0)
if sys.argv[1:] == ["-m", "pki.ratls_runtime", "origin"]:
    (root / "origin-resolved").touch()
    if os.environ.get("ORIGIN_RESOLVER_FAIL") == "1":
        raise SystemExit(3)
    print(os.environ["STARTUP_TEST_ORIGIN"])
    raise SystemExit(0)
if sys.argv[1:] == ["-"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["-m", "pki.runtime", "run"]:
    (root / "receiver.json").write_text(json.dumps({"TEE_INFERENCE_ORIGIN": os.environ.get("TEE_INFERENCE_ORIGIN")}))
while True:
    time.sleep(1)
"""


class RatlsStartupTests(unittest.TestCase):
    def run_worker(self, *, mode="ratls", fail=False):
        with tempfile.TemporaryDirectory(prefix="vita-startup-") as temporary:
            root = Path(temporary)
            (root / "node_server/data").mkdir(parents=True)
            binaries = root / "bin"
            binaries.mkdir()
            for name in ("python-stub", "node", "curl"):
                path = binaries / name
                path.write_text(f"#!{sys.executable}\n" + STUB)
                path.chmod(0o755)
            fixture = root / "fixture"
            fixture.write_text("test dataset and model")
            script = root / "start.sh"
            script.write_text(START_SCRIPT.read_text().replace("/dfl", str(root)))
            environment = os.environ.copy()
            environment.update(
                PYTHON_BIN=str(binaries / "python-stub"),
                PATH=str(binaries) + os.pathsep + environment.get("PATH", ""),
                STARTUP_TEST_ROOT=str(root),
                STARTUP_TEST_ORIGIN=ORIGIN,
                ORIGIN_RESOLVER_FAIL="1" if fail else "0",
                DOCKER="phala",
                TEE_INFERENCE_ENABLED="true",
                TEE_TRANSPORT_MODE=mode,
                TEE_INFERENCE_ORIGIN="" if mode.strip().lower() == "ratls" else "https://legacy.example.test",
                DATASET_NAME="chestmnist",
                TRAIN_DATA_SRC=str(fixture),
                TEST_DATA_SRC=str(fixture),
                BOOTSTRAP_MODEL_SRC=str(fixture),
                PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH=str(root / "participant-private.pem"),
                PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH=str(root / "participant-public.pem"),
                TEE_MODEL_DIR=str(root / "model"),
                TEE_JOB_DIR=str(root / "jobs"),
            )
            result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=10)
            records = {
                name: json.loads((root / f"{name}.json").read_text())
                for name in ("node", "receiver")
                if (root / f"{name}.json").exists()
            }
            return result, records, (root / "origin-resolved").exists()

    def test_node_registration_and_receiver_inherit_the_same_resolved_origin(self):
        for mode in ("ratls", " RaTlS "):
            with self.subTest(mode=mode):
                result, records, resolved = self.run_worker(mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(resolved)
                self.assertEqual(set(records), {"node", "receiver"})
                for environment in records.values():
                    self.assertEqual(environment["TEE_INFERENCE_ORIGIN"], ORIGIN)

    def test_origin_resolution_failure_stops_before_node_or_receiver_start(self):
        result, records, resolved = self.run_worker(fail=True)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertTrue(resolved)
        self.assertEqual(records, {})

    def test_legacy_mtls_does_not_resolve_a_dstack_origin(self):
        result, records, resolved = self.run_worker(mode="mtls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(resolved)
        for environment in records.values():
            self.assertEqual(environment["TEE_INFERENCE_ORIGIN"], "https://legacy.example.test")


if __name__ == "__main__":
    unittest.main()
