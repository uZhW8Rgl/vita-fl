"""Keep contract identity stable when restarting the persisted Anvil runtime.

This wrapper is opt-in via the key-service runtime overlay. Partial/lost state
requires operator recovery; it never redeploys over an existing registry.
"""

import hashlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path


def rpc(method, params):
    request = urllib.request.Request(
        os.environ["RPC_URL"],
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if "error" in value:
        raise RuntimeError("Persistent runtime RPC failed")
    return value["result"]


def main():
    state = Path("/bootstrap/contracts.json")
    registry = os.environ["KEY_SERVICE_REGISTRY"].lower()
    if int(rpc("eth_chainId", []), 16) != 31337:
        raise RuntimeError("Persistent runtime requires Anvil 31337")
    # Configuration changes require an explicit migration, never a silent reset.
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                name: os.environ.get(name, "")
                for name in (
                    "EXPECTED_WORKER_IMAGE",
                    "TDX_REFERENCE_QUOTE_PATH",
                    "TRAINING_WORKER_POLICY_APP_COMPOSE_B64",
                    "INFERENCE_WORKER_POLICY_APP_COMPOSE_B64",
                )
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if state.exists():
        saved = json.loads(state.read_text())
        if saved["registry"] != registry or saved["configuration"] != fingerprint:
            raise RuntimeError("Persistent bootstrap configuration changed")
        for address, digest in saved["code"].items():
            code = rpc("eth_getCode", [address, "latest"])
            if code == "0x" or hashlib.sha256(bytes.fromhex(code[2:])).hexdigest() != digest:
                raise RuntimeError("Persisted Anvil chain missing or changed")
        print("Persistent contract deployment retained; no redeployment.", flush=True)
        return
    if rpc("eth_getCode", [registry, "latest"]) != "0x":
        raise RuntimeError("Existing registry without bootstrap receipt; manual recovery required")
    subprocess.run(["bash", "/dfl/smart_contracts/starter_docker.sh"], check=True)
    request = urllib.request.Request(
        os.environ["KUBO_API_URL"] + "/api/v0/files/read?arg=/runtime/contracts.json", method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        manifest = json.load(response)
    if manifest["registry_address"].lower() != registry:
        raise RuntimeError("Unexpected registry after bootstrap")
    code = {}
    for name, value in manifest.items():
        if name.endswith("_address"):
            deployed = rpc("eth_getCode", [value, "latest"])
            if deployed == "0x":
                raise RuntimeError("Missing deployed contract")
            code[value] = hashlib.sha256(bytes.fromhex(deployed[2:])).hexdigest()
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with state.open("x") as target:
        json.dump({"registry": registry, "configuration": fingerprint, "code": code}, target)
        target.flush()
        os.fsync(target.fileno())


if __name__ == "__main__":
    main()
