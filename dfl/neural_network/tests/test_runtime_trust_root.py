from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
START_SCRIPT = ROOT / "dfl" / "start_node_neural_network.sh"
REGISTRY = "0x" + "11" * 20
AGGREGATOR = "0x" + "22" * 20
GM_STORAGE = "0x" + "33" * 20
MEDICAL_SIGNERS = "0x" + "44" * 20
CHAIN_ID = 31337


def _resolver_source() -> str:
    source = START_SCRIPT.read_text(encoding="utf-8")
    blocks = re.findall(r'"\$\{PYTHON_BIN\}" - <<\'PY\'\n(.*?)\nPY', source, re.DOTALL)
    resolver = [block for block in blocks if "expected_contracts" in block]
    if len(resolver) != 1:
        raise AssertionError("could not identify the runtime trust-root resolver")
    return resolver[0]


class _RuntimeHandler(BaseHTTPRequestHandler):
    manifest: dict[str, object] = {}
    chain_id = CHAIN_ID

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v0/files/read":
            path = urllib.parse.parse_qs(parsed.query).get("arg", [""])[0]
            if path == "/runtime/ready.json":
                self._json({"status": "ready", "chain_id": self.chain_id})
                return
            if path == "/runtime/contracts.json":
                self._json(self.manifest)
                return
        if parsed.path == "/":
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            if request["method"] == "eth_chainId":
                self._json({"jsonrpc": "2.0", "id": request["id"], "result": hex(self.chain_id)})
                return
            if request["method"] == "eth_getCode":
                self._json({"jsonrpc": "2.0", "id": request["id"], "result": "0x6000"})
                return
        self.send_error(404)

    def _json(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class RuntimeTrustRootTests(unittest.TestCase):
    def setUp(self) -> None:
        _RuntimeHandler.chain_id = CHAIN_ID
        _RuntimeHandler.manifest = {
            "registry_address": REGISTRY,
            "aggregator_address": AGGREGATOR,
            "gm_storage_address": GM_STORAGE,
            "medical_signer_registry_address": MEDICAL_SIGNERS,
            "chain_id": CHAIN_ID,
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RuntimeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _run_resolver(self) -> str:
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        environment = {
            "RPC_URL": endpoint,
            "KUBO_API": endpoint,
            "REGISTRY_ADDRESS": "",
            "AGGREGATOR_ADDRESS": "",
            "GM_STORAGE_ADDRESS": "",
            "MEDICAL_SIGNER_REGISTRY_ADDRESS": "",
            "EXPECTED_DEVICE_REGISTRY_ADDRESS": REGISTRY,
            "EXPECTED_AGGREGATOR_ADDRESS": AGGREGATOR,
            "EXPECTED_GM_STORAGE_ADDRESS": GM_STORAGE,
            "EXPECTED_MEDICAL_SIGNER_REGISTRY_ADDRESS": MEDICAL_SIGNERS,
            "EXPECTED_CHAIN_ID": str(CHAIN_ID),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime_env = Path(directory) / "runtime.env"
            environment["RUNTIME_ENV_FILE"] = str(runtime_env)
            with patch.dict(os.environ, environment, clear=False):
                exec(compile(_resolver_source(), str(START_SCRIPT), "exec"), {})
            return runtime_env.read_text(encoding="utf-8")

    def test_accepts_matching_manifest_addresses_and_rpc_chain(self) -> None:
        runtime_env = self._run_resolver()

        self.assertIn(f"export REGISTRY_ADDRESS={REGISTRY}", runtime_env)
        self.assertIn(f"export AGGREGATOR_ADDRESS={AGGREGATOR}", runtime_env)
        self.assertIn(f"export GM_STORAGE_ADDRESS={GM_STORAGE}", runtime_env)
        self.assertIn(
            f"export MEDICAL_SIGNER_REGISTRY_ADDRESS={MEDICAL_SIGNERS}",
            runtime_env,
        )

    def test_rejects_substituted_runtime_manifest_before_node_start(self) -> None:
        _RuntimeHandler.manifest["gm_storage_address"] = "0x" + "55" * 20

        with self.assertRaisesRegex(SystemExit, "does not match measured"):
            self._run_resolver()

    def test_rejects_rpc_chain_substitution_before_node_start(self) -> None:
        _RuntimeHandler.chain_id = 1
        _RuntimeHandler.manifest["chain_id"] = 1

        with self.assertRaisesRegex(SystemExit, "RPC chain ID"):
            self._run_resolver()


if __name__ == "__main__":
    unittest.main()
