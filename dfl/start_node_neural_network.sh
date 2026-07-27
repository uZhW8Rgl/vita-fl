#!/usr/bin/env bash
set -euo pipefail

PYTHON_PID=""
TEE_INFERENCE_PID=""
NODE_PID=""
RUNTIME_ENV_FILE=""

cleanup() {
    local status=$?
    trap - INT TERM EXIT
    local pid
    for pid in "${NODE_PID}" "${TEE_INFERENCE_PID}" "${PYTHON_PID}"; do
        if [ -n "${pid}" ]; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
    for pid in "${NODE_PID}" "${TEE_INFERENCE_PID}" "${PYTHON_PID}"; do
        if [ -n "${pid}" ]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
    if [ -n "${RUNTIME_ENV_FILE}" ]; then
        rm -f "${RUNTIME_ENV_FILE}"
    fi
    exit "${status}"
}

terminate() {
    exit 143
}

trap terminate INT TERM
trap cleanup EXIT

inference_enabled() {
    case "${TEE_INFERENCE_ENABLED:-0}" in
        1|true|TRUE|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH=${PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH:-/run/vita-fl/participant-private.pem}
PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH=${PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH:-/run/vita-fl/participant-public.pem}
if [ "${DOCKER:-}" != "phala" ] && [ "${PARTICIPANT_KEY_PROVIDER:-}" = "file" ]; then
    export PARTICIPANT_RSA_PRIVATE_KEY_FILE=${PARTICIPANT_RSA_PRIVATE_KEY_FILE:-${RSA_PRIVATE_KEY_FILE:-}}
fi
rm -f "${PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH}" "${PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH}"

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

rpc_url = (os.environ.get("RPC_URL") or "").strip()
contracts = {
    "REGISTRY_ADDRESS": os.environ.get("REGISTRY_ADDRESS", "").strip(),
    "AGGREGATOR_ADDRESS": os.environ.get("AGGREGATOR_ADDRESS", "").strip(),
    "GM_STORAGE_ADDRESS": os.environ.get("GM_STORAGE_ADDRESS", "").strip(),
    "MEDICAL_SIGNER_REGISTRY_ADDRESS": os.environ.get("MEDICAL_SIGNER_REGISTRY_ADDRESS", "").strip(),
}
expected_contracts = {
    "REGISTRY_ADDRESS": os.environ.get("EXPECTED_DEVICE_REGISTRY_ADDRESS", "").strip(),
    "AGGREGATOR_ADDRESS": os.environ.get("EXPECTED_AGGREGATOR_ADDRESS", "").strip(),
    "GM_STORAGE_ADDRESS": os.environ.get("EXPECTED_GM_STORAGE_ADDRESS", "").strip(),
    "MEDICAL_SIGNER_REGISTRY_ADDRESS": os.environ.get(
        "EXPECTED_MEDICAL_SIGNER_REGISTRY_ADDRESS",
        "",
    ).strip(),
}
expected_chain_id_text = os.environ.get("EXPECTED_CHAIN_ID", "").strip()
if any(missing(value) for value in expected_contracts.values()) or missing(expected_chain_id_text):
    raise SystemExit(
        "Compose-measured EXPECTED_* contract addresses and EXPECTED_CHAIN_ID are required"
    )

kubo_api = (os.environ.get("KUBO_API") or "").rstrip("/")
admission_ready = None
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

    print("Waiting for contract runtime admission marker ...", flush=True)
    admission_ready = read_mfs_json(
        "/runtime/admission-ready.json",
        lambda value: value.get("status") == "admission-ready",
    )
    manifest = read_mfs_json(
        "/runtime/contracts.json",
        lambda value: all(
            not missing(value.get(key))
            for key in (
                "registry_address",
                "aggregator_address",
                "gm_storage_address",
                "medical_signer_registry_address",
            )
        ),
    )

    for env_name, manifest_key in (
        ("REGISTRY_ADDRESS", "registry_address"),
        ("AGGREGATOR_ADDRESS", "aggregator_address"),
        ("GM_STORAGE_ADDRESS", "gm_storage_address"),
        ("MEDICAL_SIGNER_REGISTRY_ADDRESS", "medical_signer_registry_address"),
    ):
        discovered = str(manifest.get(manifest_key, "")).strip()
        if discovered.lower() != expected_contracts[env_name].lower():
            raise SystemExit(
                f"{manifest_key} from runtime manifest does not match measured {env_name} policy"
            )

    rpc_url = rpc_url if not missing(rpc_url) else str(manifest.get("rpc_url", "")).strip()
    for env_name, manifest_key in (
        ("REGISTRY_ADDRESS", "registry_address"),
        ("AGGREGATOR_ADDRESS", "aggregator_address"),
        ("GM_STORAGE_ADDRESS", "gm_storage_address"),
        ("MEDICAL_SIGNER_REGISTRY_ADDRESS", "medical_signer_registry_address"),
    ):
        if missing(contracts[env_name]):
            resolved = str(manifest.get(manifest_key, "")).strip()
            if resolved:
                contracts[env_name] = resolved
                os.environ[env_name] = resolved
    if rpc_url:
        os.environ["RPC_URL"] = rpc_url

if not rpc_url or not all(contracts.values()):
    raise SystemExit("Runtime RPC URL or contract addresses are missing")

if not re.match(r"^https?://[A-Za-z0-9._:/-]+$", rpc_url):
    raise SystemExit(f"Invalid runtime RPC URL: {rpc_url}")
for name, address in expected_contracts.items():
    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        raise SystemExit(f"Invalid measured {name} policy: {address}")
for name, address in contracts.items():
    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        raise SystemExit(f"Invalid {name}: {address}")
    if address.lower() != expected_contracts[name].lower():
        raise SystemExit(f"{name} does not match its compose-measured trust-root policy")

try:
    expected_chain_id = int(expected_chain_id_text, 0)
except ValueError as exc:
    raise SystemExit("EXPECTED_CHAIN_ID must be a positive integer") from exc
if expected_chain_id <= 0:
    raise SystemExit("EXPECTED_CHAIN_ID must be a positive integer")

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

live_chain_id = int(str(rpc("eth_chainId", []) or "0x0"), 16)
if live_chain_id != expected_chain_id:
    raise SystemExit(
        f"RPC chain ID {live_chain_id} does not match measured policy {expected_chain_id}"
    )
for source, value in (
    ("runtime admission marker", admission_ready),
    ("runtime contract manifest", manifest),
):
    if value is None:
        continue
    source_chain_id = str(value.get("chain_id", "")).strip()
    if source_chain_id:
        try:
            parsed_source_chain_id = int(source_chain_id, 0)
        except ValueError as exc:
            raise SystemExit(f"{source} contains an invalid chain ID") from exc
        if parsed_source_chain_id != live_chain_id:
            raise SystemExit(
                f"Chain ID mismatch: {source} has {parsed_source_chain_id}, "
                f"RPC has {live_chain_id}"
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
    "RPC_URL": rpc_url,
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
RUNTIME_ENV_FILE=""

# The Node process is the single owner of participant-key initialization. On
# Phala it derives the wrapping key through dstack.sock, unseals or creates the
# RSA key, and materializes only run-scoped PEM files for the co-located
# services. Local development follows the same runtime-file boundary after
# loading its explicitly configured fixture key.
export RSA_PRIVATE_KEY_FILE="${PARTICIPANT_PRIVATE_KEY_RUNTIME_PATH}"
export RSA_PUBLIC_KEY_FILE="${PARTICIPANT_PUBLIC_KEY_RUNTIME_PATH}"

node /dfl/node_server/dist/server.js &
NODE_PID=$!

if inference_enabled; then
    key_deadline=$((SECONDS + 60))
    while [ ! -s "${RSA_PRIVATE_KEY_FILE}" ] || [ ! -s "${RSA_PUBLIC_KEY_FILE}" ]; do
        if ! kill -0 "${NODE_PID}" 2>/dev/null; then
            set +e
            wait "${NODE_PID}"
            node_status=$?
            set -e
            NODE_PID=""
            echo "DFL worker exited before materializing its participant key." >&2
            [ "${node_status}" -ne 0 ] || node_status=1
            exit "${node_status}"
        fi
        if [ "${SECONDS}" -ge "${key_deadline}" ]; then
            echo "Timed out waiting for the DFL worker to materialize its participant key." >&2
            exit 1
        fi
        sleep 0.2
    done

    mkdir -p "${TEE_MODEL_DIR:-/tmp/tee-inference/model}" "${TEE_JOB_DIR:-/tmp/tee-inference/jobs}"
    "${PYTHON_BIN}" -m tee_inference.service &
    TEE_INFERENCE_PID=$!

    "${PYTHON_BIN}" - <<'PY'
import time
import urllib.request

deadline = time.time() + 60
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=2) as response:
            if response.status == 200:
                raise SystemExit(0)
    except Exception:
        time.sleep(1)
raise SystemExit("TEE inference service did not become healthy")
PY
    echo "TEE inference service listening on http://0.0.0.0:8080 (model loading remains tool-triggered)."
fi

if inference_enabled; then
    set +e
    wait -n "${NODE_PID}" "${PYTHON_PID}" "${TEE_INFERENCE_PID}"
    first_status=$?
    set -e

    if ! kill -0 "${PYTHON_PID}" 2>/dev/null; then
        PYTHON_PID=""
        echo "Python ML service exited while the combined worker was running." >&2
        [ "${first_status}" -ne 0 ] || first_status=1
        exit "${first_status}"
    fi
    if ! kill -0 "${TEE_INFERENCE_PID}" 2>/dev/null; then
        TEE_INFERENCE_PID=""
        echo "TEE inference service exited while the combined worker was running." >&2
        [ "${first_status}" -ne 0 ] || first_status=1
        exit "${first_status}"
    fi
    if kill -0 "${NODE_PID}" 2>/dev/null; then
        echo "Combined worker supervisor lost an unknown child process." >&2
        exit 1
    fi

    NODE_PID=""
    if [ "${first_status}" -ne 0 ]; then
        echo "DFL worker process exited with status ${first_status}." >&2
        exit "${first_status}"
    fi

    echo "DFL training process completed; stopping the training-only Python service."
    kill "${PYTHON_PID}" 2>/dev/null || true
    wait "${PYTHON_PID}" 2>/dev/null || true
    PYTHON_PID=""
    echo "Keeping only the TEE inference receiver available."
    set +e
    wait "${TEE_INFERENCE_PID}"
    inference_status=$?
    set -e
    TEE_INFERENCE_PID=""
    exit "${inference_status}"
fi

set +e
wait -n "${NODE_PID}" "${PYTHON_PID}"
first_status=$?
set -e
if ! kill -0 "${PYTHON_PID}" 2>/dev/null; then
    PYTHON_PID=""
    echo "Python ML service exited while the DFL worker was running." >&2
    [ "${first_status}" -ne 0 ] || first_status=1
    exit "${first_status}"
fi
NODE_PID=""
exit "${first_status}"
