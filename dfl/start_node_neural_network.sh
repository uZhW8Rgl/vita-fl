#!/usr/bin/env bash
set -euo pipefail

cleanup() {
    if [ -n "${PYTHON_PID:-}" ]; then
        kill "$PYTHON_PID" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

DATASET_NAME=${DATASET_NAME:-mnist}
BOOTSTRAP_MODEL_SRC=${BOOTSTRAP_MODEL_SRC:-/dfl/initial_gm/${DATASET_NAME}/aggregated.bin}
PYTHON_BIN=${PYTHON_BIN:-/opt/venv/bin/python}
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN=$(command -v python3 || true)
fi
if [ -z "${PYTHON_BIN}" ]; then
  echo "Neither /opt/venv/bin/python nor python3 is available in the worker container." >&2
  exit 1
fi

if [ "${DOCKER:-}" != "phala" ]; then
  export TDX_QUOTE_PATH=${TDX_QUOTE_PATH:-/dfl/node_server/attestation/phala_tdx_quote}
  if [ ! -f "${TDX_QUOTE_PATH}" ] && [ -f "/dfl/node_server/attestation/phala_tdx_quote" ]; then
    export TDX_QUOTE_PATH=/dfl/node_server/attestation/phala_tdx_quote
  fi
fi

case "$DATASET_NAME" in
  mnist)
    cp "${TRAIN_IMAGES_SRC}" /dfl/node_server/data/train-images.idx3-ubyte
    cp "${TRAIN_LABELS_SRC}" /dfl/node_server/data/train-labels.idx1-ubyte
    cp "${TEST_IMAGES_SRC:-/dfl/config/test_data/t10k-images.idx3-ubyte}" /dfl/node_server/data/t10k-images.idx3-ubyte
    cp "${TEST_LABELS_SRC:-/dfl/config/test_data/t10k-labels.idx1-ubyte}" /dfl/node_server/data/t10k-labels.idx1-ubyte
    ;;
  chestmnist)
    cp "${TRAIN_DATA_SRC}" /dfl/node_server/data/train-data.npz
    cp "${TEST_DATA_SRC}" /dfl/node_server/data/test-data.npz
    ;;
  *)
    echo "Unsupported DATASET_NAME: ${DATASET_NAME}" >&2
    exit 1
    ;;
esac

cp "${BOOTSTRAP_MODEL_SRC}" /dfl/node_server/data/random_start.bin
if [ -n "${RSA_PRIVATE_KEY:-}" ] || [ -n "${RSA_PUBLIC_KEY:-}" ]; then
    "${PYTHON_BIN}" - <<'PY'
import os
from pathlib import Path

for env_name, out_path in (
    ("RSA_PRIVATE_KEY", "/dfl/node_server/private_key.pem"),
    ("RSA_PUBLIC_KEY", "/dfl/node_server/public_key.pem"),
):
    value = os.environ.get(env_name)
    if value:
        Path(out_path).write_text(value.replace("\\n", "\n"), encoding="utf-8")
PY
else
    cp "${RSA_PRIVATE_KEY_FILE}" /dfl/node_server/private_key.pem
    cp "${RSA_PUBLIC_KEY_FILE}" /dfl/node_server/public_key.pem
fi

"${PYTHON_BIN}" /dfl/neural_network/start_service.py 2> >(grep -v "Could not initialize NNPACK" >&2) &
PYTHON_PID=$!

"${PYTHON_BIN}" - <<'PY'
import time
import urllib.request

deadline = time.time() + 60
while time.time() < deadline:
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2).read()
        raise SystemExit(0)
    except Exception:
        time.sleep(1)
raise SystemExit("Python ML service did not become healthy")
PY

RUNTIME_ENV_FILE=$(mktemp)
export RUNTIME_ENV_FILE
trap 'rm -f "${RUNTIME_ENV_FILE}"' EXIT

"${PYTHON_BIN}" - <<'PY'
import json
import os
import re
import shlex
import time
import urllib.parse
import urllib.request
from pathlib import Path

def missing(value):
    value = (value or "").strip()
    return not value or value.startswith("REPLACE_WITH_")

rpc_url = (os.environ.get("SEPOLIA_RPC_URL") or "").strip()
contracts = {
    "REGISTRY_ADDRESS": os.environ.get("REGISTRY_ADDRESS", "").strip(),
    "AGGREGATOR_ADDRESS": os.environ.get("AGGREGATOR_ADDRESS", "").strip(),
    "GM_STORAGE_ADDRESS": os.environ.get("GM_STORAGE_ADDRESS", "").strip(),
}

kubo_api = (os.environ.get("KUBO_API") or "").rstrip("/")
runtime_ready = None
manifest = None
if kubo_api:
    manifest_deadline = time.time() + 600

    def read_mfs_json(path, validator):
        url = kubo_api + "/api/v0/files/read?arg=" + urllib.parse.quote(path, safe="")
        while time.time() < manifest_deadline:
            try:
                request = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(request, timeout=5) as response:
                    value = json.loads(response.read().decode("utf-8"))
                if validator(value):
                    return value
            except Exception:
                pass
            time.sleep(2)
        raise SystemExit(f"Timed out waiting for valid runtime data via {url}")

    print("Waiting for contract runtime ready marker ...", flush=True)
    runtime_ready = read_mfs_json(
        "/runtime/ready.json",
        lambda value: value.get("status") == "ready",
    )
    manifest = read_mfs_json(
        "/runtime/contracts.json",
        lambda value: all(
            not missing(value.get(key))
            for key in (
                "registry_address",
                "aggregator_address",
                "gm_storage_address",
            )
        ),
    )

    rpc_url = rpc_url if not missing(rpc_url) else str(manifest.get("rpc_url", "")).strip()
    for env_name, manifest_key in (
        ("REGISTRY_ADDRESS", "registry_address"),
        ("AGGREGATOR_ADDRESS", "aggregator_address"),
        ("GM_STORAGE_ADDRESS", "gm_storage_address"),
    ):
        if missing(contracts[env_name]):
            resolved = str(manifest.get(manifest_key, "")).strip()
            if resolved:
                contracts[env_name] = resolved
                os.environ[env_name] = resolved
    if rpc_url:
        os.environ["SEPOLIA_RPC_URL"] = rpc_url

if not rpc_url or not all(contracts.values()):
    raise SystemExit("Runtime RPC URL or contract addresses are missing")

if not re.match(r"^https?://[A-Za-z0-9._:/-]+$", rpc_url):
    raise SystemExit(f"Invalid runtime RPC URL: {rpc_url}")
for name, address in contracts.items():
    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        raise SystemExit(f"Invalid {name}: {address}")

def rpc(method, params):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(body["error"])
    return body.get("result")

live_chain_id = str(int(str(rpc("eth_chainId", []) or "0x0"), 16))
for source, value in (
    ("runtime ready marker", runtime_ready),
    ("runtime contract manifest", manifest),
):
    if value is None:
        continue
    expected_chain_id = str(value.get("chain_id", "")).strip()
    if expected_chain_id and expected_chain_id != live_chain_id:
        raise SystemExit(
            f"Chain ID mismatch: {source} has {expected_chain_id}, RPC has {live_chain_id}"
        )

deadline = time.time() + 180
last_missing = list(contracts)
ready = False
while time.time() < deadline:
    try:
        all_ready = True
        current_missing = []
        for name, address in contracts.items():
            code = str(rpc("eth_getCode", [address, "latest"]) or "0x")
            if code in ("0x", "0x0", ""):
                all_ready = False
                current_missing.append(name)
        if current_missing:
            last_missing = current_missing
        if all_ready:
            ready = True
            break
    except Exception:
        pass
    time.sleep(2)

if not ready:
    raise SystemExit(
        f"Timed out waiting for deployed contracts on {rpc_url}: {', '.join(last_missing)}"
    )

runtime_env = {
    "SEPOLIA_RPC_URL": rpc_url,
    **contracts,
}
Path(os.environ["RUNTIME_ENV_FILE"]).write_text(
    "".join(f"export {name}={shlex.quote(value)}\n" for name, value in runtime_env.items()),
    encoding="utf-8",
)
PY

# The manifest resolver runs in a child process, so explicitly import its
# validated values into the shell environment inherited by Node.
source "${RUNTIME_ENV_FILE}"
rm -f "${RUNTIME_ENV_FILE}"
trap - EXIT

exec node /dfl/node_server/dist/server.js
