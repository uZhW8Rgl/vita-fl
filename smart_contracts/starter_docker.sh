#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DFL_ENV_FILE=${DFL_ENV_FILE:-/dfl/.env}

if [ -f "$DFL_ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$DFL_ENV_FILE"
    set +a
fi

BLOCKCHAIN_PROVIDER=${BLOCKCHAIN_PROVIDER:-anvil}
KUBO_API_URL=${KUBO_API_URL:-http://ipfs:5001}
IPFS_PROVIDER=${IPFS_PROVIDER:-kubo}
ETH_WALLET_PRIVATE_KEY=${ETH_WALLET_PRIVATE_KEY:-}

require_private_key() {
    local name=$1
    local value=$2

    if ! printf '%s' "$value" | grep -Eq '^0x[0-9a-fA-F]{64}$'; then
        echo "Invalid $name private key"
        exit 1
    fi
}

if [ -z "$ETH_WALLET_PRIVATE_KEY" ]; then
    ETH_WALLET_PRIVATE_KEY=${W0_PRIVATE_KEY:-}
fi
require_private_key ETH_WALLET_PRIVATE_KEY "$ETH_WALLET_PRIVATE_KEY"
export ETH_WALLET_PRIVATE_KEY
DEPLOYER_ADDRESS=$(cast wallet address --private-key "$ETH_WALLET_PRIVATE_KEY")
export DEPLOYER_ADDRESS

if [ -n "${RPC_URL:-}" ]; then
    rpc_url=$RPC_URL
elif [ "${DOCKER:-}" = "phala" ]; then
    rpc_url=https://61ecc557e3b36593390057d322d46e9488032c34-8545.dstack-prod5.phala.network
elif [ "$BLOCKCHAIN_PROVIDER" = "anvil" ]; then
    rpc_url=http://anvil:8545
else
    echo "RPC_URL must be set when BLOCKCHAIN_PROVIDER=$BLOCKCHAIN_PROVIDER"
    exit 1
fi
export RPC_URL=$rpc_url

FORGE_REMOTE_FLAGS=()
if [ "$BLOCKCHAIN_PROVIDER" != "anvil" ]; then
    # Public Sepolia RPC endpoints are noticeably less tolerant of batched nonce usage.
    # `--slow` makes forge wait for confirmations between broadcasts and avoids
    # replacement-underpriced races during multi-transaction deployments.
    FORGE_REMOTE_FLAGS+=(--slow)
fi

wait_for_anvil() {
    local attempts=${1:-60}
    local rpc_endpoint=$rpc_url

    echo "Waiting for JSON-RPC endpoint at ${rpc_endpoint}..."
    for _ in $(seq 1 "$attempts"); do
        if cast chain-id --rpc-url "$rpc_endpoint" >/dev/null 2>&1; then
            echo "JSON-RPC endpoint is ready."
            return 0
        fi
        sleep 2
    done

    echo "Timed out waiting for JSON-RPC endpoint at ${rpc_endpoint}."
    return 1
}

wait_for_kubo() {
    local attempts=${1:-60}

    if [ "$IPFS_PROVIDER" != "kubo" ]; then
        return 0
    fi

    echo "Waiting for Kubo at ${KUBO_API_URL}..."
    for _ in $(seq 1 "$attempts"); do
        if curl --connect-timeout 2 --max-time 5 -fsS -X POST "${KUBO_API_URL}/api/v0/version" >/dev/null 2>&1; then
            echo "Kubo is ready."
            return 0
        fi
        sleep 2
    done

    echo "Timed out waiting for Kubo at ${KUBO_API_URL}."
    return 1
}

wait_for_anvil
wait_for_kubo

CHAIN_ID=$(cast chain-id --rpc-url $rpc_url)
ETH_EUR_PRICE=${ETH_EUR_PRICE:-3000}

require_address() {
    local name=$1
    local value=$2

    if ! printf '%s' "$value" | grep -Eq '^0x[0-9a-fA-F]{40}$'; then
        echo "Invalid $name address: $value"
        exit 1
    fi
}

update_env_var() {
    local key=$1
    local value=$2
    local env_file=${DFL_ENV_FILE:-/dfl/.env}

    if [ ! -f "$env_file" ]; then
        return 0
    fi

    python3 - "$env_file" "$key" "$value" <<'PY'
from pathlib import Path
import re
import sys

env_path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = env_path.read_text(encoding="utf-8").splitlines()
pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
for index, line in enumerate(lines):
    if pattern.match(line):
        lines[index] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

has_contract_code() {
    local address=$1
    local code
    code=$(cast code --rpc-url "$rpc_url" "$address" 2>/dev/null || true)
    [ -n "$code" ] && [ "$code" != "0x" ]
}

ensure_pccs_deployment_file() {
    local deployment_file=$1
    mkdir -p "$(dirname "$deployment_file")"
    python3 - "$deployment_file" <<'PY'
from pathlib import Path
import json
import os
import sys

path = Path(sys.argv[1])
payload = {
    "EnclaveIdentityHelper": os.environ.get("ENCLAVE_IDENTITY_HELPER", ""),
    "FmspcTcbHelper": os.environ.get("FMSPC_TCB_HELPER", ""),
    "PCKHelper": os.environ.get("X509_HELPER", ""),
    "X509CRLHelper": os.environ.get("X509_CRL_HELPER", ""),
    "AutomataDaoStorage": os.environ.get("PCCS_STORAGE", ""),
    "AutomataPcsDao": os.environ.get("PCS_DAO", ""),
    "AutomataEnclaveIdentityDao": os.environ.get("ENCLAVE_ID_DAO", ""),
    "AutomataFmspcTcbDao": os.environ.get("FMSPC_TCB_DAO", ""),
}
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

authorize_pccs_reader() {
    local caller=$1

    if [ -z "$caller" ]; then
        return 0
    fi

    require_address PCCS_READER "$caller"

    pushd "$PCCS_ROOT" >/dev/null || {
        echo "Failed to enter PCCS root: $PCCS_ROOT"
        exit 1
    }
    if ! forge script script/automata/ConfigAutomataDao.s.sol \
        --sig "setAuthorizedCaller(address,bool)" "$caller" true \
        --broadcast --rpc-url $rpc_url; then
        echo "Failed to authorize PCCS reader: $caller"
        popd >/dev/null
        exit 1
    fi
    popd >/dev/null
}

log_broadcast_gas_cost() {
    local phase=$1
    local broadcast_file=$2

    if [ ! -f "$broadcast_file" ]; then
        return 0
    fi

    ETH_EUR_PRICE="$ETH_EUR_PRICE" PHASE="$phase" TRANSACTION_COST_CSV="${TRANSACTION_COST_CSV:-/dfl/data/evaluation/transaction_costs.csv}" python3 - "$broadcast_file" <<'PY' || true
import csv
import json
import os
import sys
import time


def to_int(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    return int(text, 16) if text.startswith(("0x", "0X")) else int(text)


with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)

eth_eur_price = float(os.environ.get("ETH_EUR_PRICE", "3000"))
phase = os.environ["PHASE"]
csv_path = os.environ.get("TRANSACTION_COST_CSV", "")
csv_header = [
    "timestamp_unix_ms",
    "scope",
    "operation",
    "phase",
    "transactionHash",
    "blockNumber",
    "from",
    "to",
    "contractAddress",
    "gasUsed",
    "effectiveGasPriceWei",
    "effectiveGasPriceGwei",
    "costEth",
    "costEur",
    "ethEurPrice",
    "account",
    "deviceId",
]

csv_rows = []

for receipt in data.get("receipts", []):
    gas_used = to_int(receipt.get("gasUsed"))
    gas_price = to_int(receipt.get("effectiveGasPrice"))
    cost_eth = gas_used * gas_price / 10**18
    event = {
        "timestamp_unix_ms": int(time.time() * 1000),
        "kind": "gas_cost",
        "scope": "smart_contracts_init",
        "phase": phase,
        "operation": "contract_deploy" if receipt.get("contractAddress") else "transaction",
        "gasUsed": gas_used,
        "effectiveGasPriceWei": gas_price,
        "effectiveGasPriceGwei": gas_price / 10**9,
        "costEth": cost_eth,
        "costEur": cost_eth * eth_eur_price,
        "ethEurPrice": eth_eur_price,
        "transactionHash": receipt.get("transactionHash"),
        "blockNumber": to_int(receipt.get("blockNumber")),
        "from": receipt.get("from"),
        "to": receipt.get("to"),
        "contractAddress": receipt.get("contractAddress"),
        "account": "",
        "deviceId": "",
    }
    print(json.dumps(event, separators=(",", ":")))
    csv_rows.append({field: event.get(field, "") for field in csv_header})

if csv_path and csv_rows:
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_header)
        if write_header:
            writer.writeheader()
        writer.writerows(csv_rows)
PY
}

log_cast_send_gas_cost() {
    local scope=$1
    local operation=$2
    local receipt_json=$3
    local account=${4:-}
    local device_id=${5:-}

    ETH_EUR_PRICE="$ETH_EUR_PRICE" \
    SCOPE="$scope" \
    OPERATION="$operation" \
    ACCOUNT="$account" \
    DEVICE_ID="$device_id" \
    TRANSACTION_COST_CSV="${TRANSACTION_COST_CSV:-/dfl/data/evaluation/transaction_costs.csv}" \
    python3 - <<'PY' "$receipt_json" || true
import csv
import json
import os
import sys
import time


def to_int(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    return int(text, 16) if text.startswith(("0x", "0X")) else int(text)


receipt = json.loads(sys.argv[1])
eth_eur_price = float(os.environ.get("ETH_EUR_PRICE", "3000"))
csv_path = os.environ.get("TRANSACTION_COST_CSV", "")
csv_header = [
    "timestamp_unix_ms",
    "scope",
    "operation",
    "phase",
    "transactionHash",
    "blockNumber",
    "from",
    "to",
    "contractAddress",
    "gasUsed",
    "effectiveGasPriceWei",
    "effectiveGasPriceGwei",
    "costEth",
    "costEur",
    "ethEurPrice",
    "account",
    "deviceId",
]

gas_used = to_int(receipt.get("gasUsed"))
gas_price = to_int(receipt.get("effectiveGasPrice"))
cost_eth = gas_used * gas_price / 10**18
event = {
    "timestamp_unix_ms": int(time.time() * 1000),
    "kind": "gas_cost",
    "scope": os.environ["SCOPE"],
    "phase": "",
    "operation": os.environ["OPERATION"],
    "gasUsed": gas_used,
    "effectiveGasPriceWei": gas_price,
    "effectiveGasPriceGwei": gas_price / 10**9,
    "costEth": cost_eth,
    "costEur": cost_eth * eth_eur_price,
    "ethEurPrice": eth_eur_price,
    "transactionHash": receipt.get("transactionHash"),
    "blockNumber": to_int(receipt.get("blockNumber")),
    "from": receipt.get("from"),
    "to": receipt.get("to"),
    "contractAddress": receipt.get("contractAddress"),
    "account": os.environ.get("ACCOUNT", ""),
    "deviceId": os.environ.get("DEVICE_ID", ""),
}
print(json.dumps(event, separators=(",", ":")))

if csv_path:
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_header)
        if write_header:
            writer.writeheader()
        writer.writerow({field: event.get(field, "") for field in csv_header})
PY
}

run_cast_send_logged() {
    local scope=$1
    local operation=$2
    local account=$3
    local device_id=$4
    shift 4
    local receipt_json
    local attempt
    local output
    for attempt in 1 2 3 4; do
        if output=$(cast send --json "$@" 2>&1); then
            receipt_json=$output
            break
        fi
        if printf '%s' "$output" | grep -Eqi 'nonce too low|replacement transaction underpriced|already known'; then
            sleep 3
            continue
        fi
        printf '%s\n' "$output" >&2
        return 1
    done
    if [ -z "${receipt_json:-}" ]; then
        printf '%s\n' "$output" >&2
        return 1
    fi
    printf '%s\n' "$receipt_json"
    log_cast_send_gas_cost "$scope" "$operation" "$receipt_json" "$account" "$device_id"
}

if [ "$IPFS_PROVIDER" != "kubo" ] && [ "$IPFS_PROVIDER" != "pinata" ]; then
    echo "Unsupported IPFS_PROVIDER: $IPFS_PROVIDER"
    exit 1
fi

add_file_to_kubo() {
    local file_path=$1
    local response

    response=$(curl --connect-timeout 5 --max-time 60 -sSf -X POST \
        -F "file=@${file_path}" \
        "${KUBO_API_URL}/api/v0/add?pin=true&cid-version=1&wrap-with-directory=false")
    printf '%s' "$response" | jq -re '.Hash'
}

add_file_to_pinata() {
    local file_path=$1
    local response

    if [ -z "${PINATA_JWT:-}" ]; then
        echo "PINATA_JWT must be set when IPFS_PROVIDER=pinata"
        exit 1
    fi

    response=$(curl --connect-timeout 10 --max-time 180 -sSf -X POST \
        -H "Authorization: Bearer ${PINATA_JWT}" \
        -F "file=@${file_path}" \
        -F 'pinataOptions={"cidVersion":1}' \
        https://api.pinata.cloud/pinning/pinFileToIPFS)
    printf '%s' "$response" | jq -re '.IpfsHash'
}

add_file_to_ipfs() {
    local file_path=$1

    case "$IPFS_PROVIDER" in
        kubo)
            add_file_to_kubo "$file_path"
            ;;
        pinata)
            add_file_to_pinata "$file_path"
            ;;
        *)
            echo "Unsupported IPFS_PROVIDER=$IPFS_PROVIDER"
            exit 1
            ;;
    esac
}

prepare_local_initial_gm() {
    if [ "${DOCKER:-}" = "phala" ]; then
        return 0
    fi

    if [ "$IPFS_PROVIDER" = "pinata" ]; then
        echo "IPFS_PROVIDER=pinata: expecting INITIAL_GM_CID and INITIAL_GM_SIG_CID from the environment"
        return 0
    fi

    local dataset_name=${DATASET_NAME:-mnist}
    local model_path=${INITIAL_GM_MODEL_PATH:-../data/initial_gm/${dataset_name}/aggregated.bin}
    local signing_key_path=${INITIAL_GM_SIGNING_KEY_PATH:-../data/initial_gm/private_key.pem}
    local signature_path=${INITIAL_GM_SIGNATURE_PATH:-/tmp/initial-gm.sig}

    if [ ! -f "$model_path" ]; then
        echo "Missing initial GM model file: $model_path"
        exit 1
    fi
    if [ ! -f "$signing_key_path" ]; then
        echo "Missing initial GM signing key: $signing_key_path"
        exit 1
    fi

    echo "Signing and importing the initial GM into the local IPFS node"
    openssl dgst -sha256 -sign "$signing_key_path" -out "$signature_path" "$model_path"

    export INITIAL_GM_CID
    export INITIAL_GM_SIG_CID
    INITIAL_GM_CID=$(add_file_to_ipfs "$model_path")
    INITIAL_GM_SIG_CID=$(add_file_to_ipfs "$signature_path")

    echo "Local initial GM CID: $INITIAL_GM_CID"
    echo "Local initial GM signature CID: $INITIAL_GM_SIG_CID"
}

prepare_encrypted_initial_gm() {
    if [ "${DOCKER:-}" = "phala" ]; then
        return 0
    fi

    local dataset_name=${DATASET_NAME:-mnist}
    local model_path=${INITIAL_GM_MODEL_PATH:-../data/initial_gm/${dataset_name}/aggregated.bin}
    local signing_key_path=${INITIAL_GM_SIGNING_KEY_PATH:-../data/initial_gm/private_key.pem}
    local signature_path=${INITIAL_GM_SIGNATURE_PATH:-/tmp/initial-gm.sig}
    local out_dir=/tmp/bootstrap-encrypted-gm
    local bootstrap_recipient_address=${INITIAL_BOOTSTRAP_RECIPIENT_ADDRESS:-${W0_ACCOUNT_ADDRESS:-}}
    local bootstrap_recipient_public_key=${INITIAL_BOOTSTRAP_RECIPIENT_PUBLIC_KEY_PATH:-../data/rsa_keys/public_key.pem}

    if [ ! -f "$model_path" ]; then
        echo "Missing initial GM model file for encrypted bootstrap: $model_path"
        exit 1
    fi
    if [ ! -f "$signing_key_path" ]; then
        echo "Missing initial GM signing key for encrypted bootstrap: $signing_key_path"
        exit 1
    fi
    if [ ! -f "$signature_path" ]; then
        echo "Generating missing local initial GM signature for encrypted bootstrap"
        openssl dgst -sha256 -sign "$signing_key_path" -out "$signature_path" "$model_path"
    fi
    if [ ! -f "$signature_path" ]; then
        echo "Missing initial GM signature file for encrypted bootstrap: $signature_path"
        exit 1
    fi

    echo "Generating encrypted initial GM bootstrap bundle"
    local bootstrap_json
    bootstrap_json=$(node ./bootstrap_encrypted_gm.mjs \
        --model "$model_path" \
        --signature "$signature_path" \
        --private-key "$signing_key_path" \
        --out-dir "$out_dir" \
        --rpc-url "$rpc_url" \
        --registry-address "$DEVICE_REGISTRY_ADDRESS" \
        --bootstrap-address "$bootstrap_recipient_address" \
        --bootstrap-public-key "$bootstrap_recipient_public_key" \
        --round 0)

    local bundle_path
    local bundle_signature_path
    local key_bundle_path
    bundle_path=$(printf '%s' "$bootstrap_json" | jq -re '.bundlePath')
    bundle_signature_path=$(printf '%s' "$bootstrap_json" | jq -re '.bundleSignaturePath')
    key_bundle_path=$(printf '%s' "$bootstrap_json" | jq -re '.keyBundlePath')
    recipient_count=$(printf '%s' "$bootstrap_json" | jq -re '.recipientCount')

    echo "Encrypted bootstrap recipients from on-chain registry: $recipient_count"

    local encrypted_model_cid
    local encrypted_sig_cid
    local encrypted_key_cid
    encrypted_model_cid=$(add_file_to_ipfs "$bundle_path")
    encrypted_sig_cid=$(add_file_to_ipfs "$bundle_signature_path")
    encrypted_key_cid=$(add_file_to_ipfs "$key_bundle_path")

    echo "Encrypted initial GM bundle CID: $encrypted_model_cid"
    echo "Encrypted initial GM signature CID: $encrypted_sig_cid"
    echo "Encrypted initial GM key bundle CID: $encrypted_key_cid"

    run_cast_send_logged "smart_contracts_init" "set_encrypted_bootstrap_bundle" "$DEPLOYER_ADDRESS" "" \
        --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
        $GMSTORAGE "setGlobalModelAndSignatureAndKeyBundle(string,string,string)" \
        "$encrypted_model_cid" "$encrypted_sig_cid" "$encrypted_key_cid"
}

prepare_local_initial_gm
forge script --rpc-url $rpc_url --broadcast "${FORGE_REMOTE_FLAGS[@]}" script/Deploy.s.sol
log_broadcast_gas_cost "deploy_core_contracts" "./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json"

export DEVICE_REGISTRY_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "DeviceRegistry") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export AGGREGATOR_SELECTION_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "AggregatorSelection") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export GMSTORAGE=$(jq -re '.transactions[] | select(.contractName == "GMStorage") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)
update_env_var REGISTRY_ADDRESS "$DEVICE_REGISTRY_ADDRESS"
update_env_var AGGREGATOR_ADDRESS "$AGGREGATOR_SELECTION_ADDRESS"
update_env_var GM_STORAGE_ADDRESS "$GMSTORAGE"

ENABLE_DCAP=${ENABLE_DCAP:-1}
if [ "$ENABLE_DCAP" = "1" ]; then
    echo "Deploying Automata DCAP v4 contracts"

    export PRIVATE_KEY=$ETH_WALLET_PRIVATE_KEY
    export DCAP_IMAGE_ID=${DCAP_IMAGE_ID:-0x97f41badbcc8d79521f10cd076fa7a2ed67b84abe07c496da11a2a708c9e5f14}

    if [ "${DOCKER:-}" != "phala" ]; then
        FALLBACK_P256_VERIFIER_ADDRESS=0xc2b78104907F722DABAc4C69f826a522B2754De4
        P256_SOURCE_PATH="lib/p256-verifier/src/P256Verifier.sol"
        NATIVE_P256_VERIFIER_ADDRESS=0x0000000000000000000000000000000000000100
        P256_RUNTIME=
        ensure_p256_runtime() {
            if [ -n "$P256_RUNTIME" ]; then
                return 0
            fi

            if [ ! -f "$SCRIPT_DIR/$P256_SOURCE_PATH" ]; then
                echo "Missing $SCRIPT_DIR/$P256_SOURCE_PATH in container. Ensure dependencies are copied into the image."
                exit 1
            fi

            forge build --force "$P256_SOURCE_PATH"

            P256_ARTIFACT="$SCRIPT_DIR/out/P256Verifier.sol/P256Verifier.json"
            if [ ! -f "$P256_ARTIFACT" ]; then
                echo "P256Verifier artifact not found after compilation: $P256_ARTIFACT"
                exit 1
            fi

            P256_RUNTIME=$(jq -r '.deployedBytecode.object // .deployedBytecode // empty' "$P256_ARTIFACT" | tr -d '\n')
            if [ -n "$P256_RUNTIME" ] && [ "${P256_RUNTIME#0x}" = "$P256_RUNTIME" ]; then
                P256_RUNTIME="0x$P256_RUNTIME"
            fi
            if [ -z "$P256_RUNTIME" ] || [ "$P256_RUNTIME" = "0x" ]; then
                echo "Failed to extract P256Verifier deployed bytecode from $P256_ARTIFACT"
                exit 1
            fi
        }

        install_p256_verifier() {
            local verifier=$1
            ensure_p256_runtime
            cast rpc --rpc-url $rpc_url anvil_setCode "$verifier" "$P256_RUNTIME"
        }

        probe_p256_route() {
            local verifier=$1
            local output
            output=$(P256_VERIFIER_ADDRESS=$verifier forge script script/ProbeP256Verifier.s.sol --rpc-url $rpc_url)
            printf '%s\n' "$output" >&2
            printf '%s\n' "$output" | sed -n 's/.*P256 effective route: //p' | tail -n 1
        }

        P256_MODE=${P256_MODE:-native}
        if [ "$P256_MODE" = "native" ]; then
            P256_ROUTE=$(probe_p256_route "$NATIVE_P256_VERIFIER_ADDRESS")
            if [ "$P256_ROUTE" = "native-precompile" ]; then
                export P256_VERIFIER_ADDRESS=$NATIVE_P256_VERIFIER_ADDRESS
                echo "Using native P256 precompile at $P256_VERIFIER_ADDRESS"
            elif [ "$P256_ROUTE" = "unavailable" ] || [ -z "$P256_ROUTE" ]; then
                echo "Native P256 precompile unavailable; installing local P256 verifier at $NATIVE_P256_VERIFIER_ADDRESS"
                install_p256_verifier "$NATIVE_P256_VERIFIER_ADDRESS"
                P256_ROUTE=$(probe_p256_route "$NATIVE_P256_VERIFIER_ADDRESS")
                if [ "$P256_ROUTE" != "native-precompile" ]; then
                    echo "Native P256 verifier unavailable at $NATIVE_P256_VERIFIER_ADDRESS"
                    exit 1
                fi
                export P256_VERIFIER_ADDRESS=$NATIVE_P256_VERIFIER_ADDRESS
                echo "Using native P256 verifier address at $P256_VERIFIER_ADDRESS"
            elif [ "${P256_REQUIRE_NATIVE:-0}" = "1" ]; then
                echo "Native P256 precompile requested but unavailable at $NATIVE_P256_VERIFIER_ADDRESS"
                exit 1
            elif [ "$P256_ROUTE" = "fallback-contract" ]; then
                export P256_VERIFIER_ADDRESS=$FALLBACK_P256_VERIFIER_ADDRESS
                echo "Native P256 precompile unavailable; using fallback P256 verifier at $P256_VERIFIER_ADDRESS"
            else
                echo "Neither native nor fallback P256 verifier is available."
                exit 1
            fi
        elif [ "$P256_MODE" = "fallback" ]; then
            install_p256_verifier "$FALLBACK_P256_VERIFIER_ADDRESS"
            P256_ROUTE=$(probe_p256_route "$FALLBACK_P256_VERIFIER_ADDRESS")
            if [ "$P256_ROUTE" != "fallback-contract" ]; then
                echo "Fallback P256 verifier unavailable at $FALLBACK_P256_VERIFIER_ADDRESS"
                exit 1
            fi
            export P256_VERIFIER_ADDRESS=$FALLBACK_P256_VERIFIER_ADDRESS
            echo "Using fallback P256 verifier at $P256_VERIFIER_ADDRESS"
        else
            export P256_VERIFIER_ADDRESS=${P256_VERIFIER_ADDRESS:-$FALLBACK_P256_VERIFIER_ADDRESS}
            if [ "$P256_VERIFIER_ADDRESS" = "$NATIVE_P256_VERIFIER_ADDRESS" ] || [ "$P256_VERIFIER_ADDRESS" = "$FALLBACK_P256_VERIFIER_ADDRESS" ]; then
                install_p256_verifier "$P256_VERIFIER_ADDRESS"
            fi
            P256_ROUTE=$(probe_p256_route "$P256_VERIFIER_ADDRESS")
            if [ "$P256_ROUTE" = "unavailable" ] || [ -z "$P256_ROUTE" ]; then
                echo "Configured P256 verifier unavailable at $P256_VERIFIER_ADDRESS"
                exit 1
            fi
            echo "Using configured P256 verifier at $P256_VERIFIER_ADDRESS"
        fi
    else
        export P256_VERIFIER_ADDRESS=${P256_VERIFIER_ADDRESS:-0xc2b78104907F722DABAc4C69f826a522B2754De4}
    fi

    PCCS_ROOT="$SCRIPT_DIR/lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs"
    pushd "$PCCS_ROOT" >/dev/null || {
        echo "Failed to enter PCCS root: $PCCS_ROOT"
        exit 1
    }

    export OWNER=$DEPLOYER_ADDRESS
    require_address OWNER "$OWNER"

    PCCS_DEPLOYMENT_FILE="./deployment/$CHAIN_ID.json"
    REUSE_PCCS_DEPLOYMENTS=${REUSE_PCCS_DEPLOYMENTS:-1}

    if [ "$REUSE_PCCS_DEPLOYMENTS" = "1" ] \
        && [ -n "${ENCLAVE_IDENTITY_HELPER:-}" ] && has_contract_code "$ENCLAVE_IDENTITY_HELPER" \
        && [ -n "${FMSPC_TCB_HELPER:-}" ] && has_contract_code "$FMSPC_TCB_HELPER" \
        && [ -n "${X509_HELPER:-}" ] && has_contract_code "$X509_HELPER" \
        && [ -n "${X509_CRL_HELPER:-}" ] && has_contract_code "$X509_CRL_HELPER" \
        && [ -n "${PCCS_STORAGE:-}" ] && has_contract_code "$PCCS_STORAGE" \
        && [ -n "${PCS_DAO:-}" ] && has_contract_code "$PCS_DAO" \
        && [ -n "${ENCLAVE_ID_DAO:-}" ] && has_contract_code "$ENCLAVE_ID_DAO" \
        && [ -n "${FMSPC_TCB_DAO:-}" ] && has_contract_code "$FMSPC_TCB_DAO"; then
        echo "Reusing existing PCCS helper and DAO deployments from the environment"
        ensure_pccs_deployment_file "$PCCS_DEPLOYMENT_FILE"
    else
        forge script script/helper/DeployHelpers.s.sol --sig "deployEnclaveIdentityHelper()" --broadcast "${FORGE_REMOTE_FLAGS[@]}" --rpc-url $rpc_url --ffi
        log_broadcast_gas_cost "deploy_enclave_identity_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployEnclaveIdentityHelper-latest.json"
        export ENCLAVE_IDENTITY_HELPER=$(jq -re '.transactions[] | select(.contractName == "EnclaveIdentityHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployEnclaveIdentityHelper-latest.json)

        forge script script/helper/DeployHelpers.s.sol --sig "deployFmspcTcbHelper()" --broadcast "${FORGE_REMOTE_FLAGS[@]}" --rpc-url $rpc_url
        log_broadcast_gas_cost "deploy_fmspc_tcb_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployFmspcTcbHelper-latest.json"
        export FMSPC_TCB_HELPER=$(jq -re '.transactions[] | select(.contractName == "FmspcTcbHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployFmspcTcbHelper-latest.json)

        forge script script/helper/DeployHelpers.s.sol --sig "deployPckHelper()" --broadcast "${FORGE_REMOTE_FLAGS[@]}" --rpc-url $rpc_url
        log_broadcast_gas_cost "deploy_pck_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployPckHelper-latest.json"
        export X509_HELPER=$(jq -re '.transactions[] | select(.contractName == "PCKHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployPckHelper-latest.json)

        forge script script/helper/DeployHelpers.s.sol --sig "deployX509CrlHelper()" --broadcast "${FORGE_REMOTE_FLAGS[@]}" --rpc-url $rpc_url
        log_broadcast_gas_cost "deploy_x509_crl_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployX509CrlHelper-latest.json"
        export X509_CRL_HELPER=$(jq -re '.transactions[] | select(.contractName == "X509CRLHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployX509CrlHelper-latest.json)

        if [ ! -f "$PCCS_DEPLOYMENT_FILE" ]; then
            echo "Missing helper deployment file before DAO deploy: $PCCS_DEPLOYMENT_FILE"
            exit 1
        fi

        if ! forge script script/automata/DeployAutomataDao.s.sol \
            --sig "deployAll(bool,bool)" true true \
            --broadcast --rpc-url $rpc_url; then
            echo "Failed to deploy Automata PCCS DAO suite"
            exit 1
        fi
        log_broadcast_gas_cost "deploy_automata_dao" "./broadcast/DeployAutomataDao.s.sol/$CHAIN_ID/run-latest.json"
    fi

    if [ ! -f "$PCCS_DEPLOYMENT_FILE" ]; then
        echo "Missing PCCS deployment file: $PCCS_DEPLOYMENT_FILE"
        exit 1
    fi
    export PCCS_STORAGE=$(jq -re '.AutomataDaoStorage' "$PCCS_DEPLOYMENT_FILE")
    export PCS_DAO=$(jq -re '.AutomataPcsDao' "$PCCS_DEPLOYMENT_FILE")
    export ENCLAVE_ID_DAO=$(jq -re '.AutomataEnclaveIdentityDao' "$PCCS_DEPLOYMENT_FILE")
    export FMSPC_TCB_DAO=$(jq -re '.AutomataFmspcTcbDao' "$PCCS_DEPLOYMENT_FILE")

    require_address PCCS_STORAGE "$PCCS_STORAGE"
    require_address PCS_DAO "$PCS_DAO"
    require_address ENCLAVE_ID_DAO "$ENCLAVE_ID_DAO"
    require_address FMSPC_TCB_DAO "$FMSPC_TCB_DAO"

    popd >/dev/null

	    DEPLOY_TDX_V4_DCAP=${DEPLOY_TDX_V4_DCAP:-1}
	    if [ "$DEPLOY_TDX_V4_DCAP" = "1" ]; then
            TDX_V4_CONTRACT_VARIANT=${TDX_V4_CONTRACT_VARIANT:-}
            if [ -z "$TDX_V4_CONTRACT_VARIANT" ]; then
                if [ "$BLOCKCHAIN_PROVIDER" = "sepolia" ]; then
                    TDX_V4_CONTRACT_VARIANT=lite
                else
                    TDX_V4_CONTRACT_VARIANT=full
                fi
            fi
            if [ "$TDX_V4_CONTRACT_VARIANT" = "lite" ]; then
                TDX_V4_CONTRACT_TARGET="src/attestation/AutomataDcapTdxV4AttestationLite.sol:AutomataDcapTdxV4AttestationLite"
                echo "Deploying lightweight TDX V4 attestation contract variant"
            else
                TDX_V4_CONTRACT_TARGET="src/attestation/AutomataDcapTdxV4Attestation.sol:AutomataDcapTdxV4Attestation"
                echo "Deploying full TDX V4 attestation contract variant"
            fi
            if [ "$TDX_V4_CONTRACT_VARIANT" = "lite" ]; then
                if [ -n "${RTMR3_REPLAY_POLICY_ADDRESS:-}" ] && has_contract_code "$RTMR3_REPLAY_POLICY_ADDRESS"; then
                    echo "Reusing existing PhalaRtmr3ReplayPolicy at $RTMR3_REPLAY_POLICY_ADDRESS"
                else
                    POLICY_CREATE_JSON=$(forge create src/attestation/PhalaRtmr3ReplayPolicy.sol:PhalaRtmr3ReplayPolicy \
                        --force --no-cache --broadcast --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY --json)
                    echo "$POLICY_CREATE_JSON"
                    export RTMR3_REPLAY_POLICY_ADDRESS=$(printf '%s' "$POLICY_CREATE_JSON" | jq -re '.deployedTo // .deployed_to')
                    echo "PhalaRtmr3ReplayPolicy: $RTMR3_REPLAY_POLICY_ADDRESS"
                    update_env_var RTMR3_REPLAY_POLICY_ADDRESS "$RTMR3_REPLAY_POLICY_ADDRESS"
                fi
            fi
            if [ -n "${DCAP_TDX_V4_ADDRESS:-}" ] && has_contract_code "$DCAP_TDX_V4_ADDRESS"; then
                echo "Reusing existing AutomataDcapTdxV4Attestation at $DCAP_TDX_V4_ADDRESS"
            else
	            TDX_CREATE_JSON=$(forge create "$TDX_V4_CONTRACT_TARGET" \
	                --force --no-cache --broadcast --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY --json \
	                --constructor-args \
	                "$ENCLAVE_ID_DAO" \
	                "$X509_HELPER" \
	                "$FMSPC_TCB_DAO" \
	                "$X509_CRL_HELPER" \
	                "$PCS_DAO" \
	                "$P256_VERIFIER_ADDRESS")
	            echo "$TDX_CREATE_JSON"
	            export DCAP_TDX_V4_ADDRESS=$(printf '%s' "$TDX_CREATE_JSON" | jq -re '.deployedTo // .deployed_to')
	            echo "AutomataDcapTdxV4Attestation: $DCAP_TDX_V4_ADDRESS"
                update_env_var DCAP_TDX_V4_ADDRESS "$DCAP_TDX_V4_ADDRESS"
            fi
            if [ "$TDX_V4_CONTRACT_VARIANT" = "lite" ]; then
                run_cast_send_logged "smart_contracts_init" "set_rtmr3_replay_policy" "$OWNER" "" \
                    --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                    $DEVICE_REGISTRY_ADDRESS "setRtmr3ReplayPolicy(address)" $RTMR3_REPLAY_POLICY_ADDRESS
            fi
            TDX_REFERENCE_QUOTE_PATH=${TDX_REFERENCE_QUOTE_PATH:-../data/phala_tdx_quote}
            if [ -f "$TDX_REFERENCE_QUOTE_PATH" ]; then
                TDX_REFERENCE_QUOTE_HEX=$(tr -d '[:space:]' < "$TDX_REFERENCE_QUOTE_PATH")
                TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0x}
                TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0X}
                echo "Configuring expected RTMR3 policy from reference quote: $TDX_REFERENCE_QUOTE_PATH"
                run_cast_send_logged "smart_contracts_init" "set_expected_rtmr3_from_quote" "$OWNER" "" \
                    --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                    $DCAP_TDX_V4_ADDRESS "setExpectedRtmr3FromQuote(bytes)" "0x$TDX_REFERENCE_QUOTE_HEX"
            fi
            PHALA_COMPOSE_PATH=${PHALA_COMPOSE_PATH:-../phala/dstack-compose.template.yml}
            if [ "$TDX_V4_CONTRACT_VARIANT" = "full" ] && [ -f "$PHALA_COMPOSE_PATH" ]; then
                if ! grep -Eq 'image:[[:space:]]*[^[:space:]]+@sha256:[0-9a-fA-F]{64}' "$PHALA_COMPOSE_PATH"; then
                    echo "Phala compose policy must pin the worker image by immutable sha256 digest: $PHALA_COMPOSE_PATH"
                    exit 1
                fi
                PHALA_APP_CODE_PATH=${PHALA_APP_CODE_PATH:-../phala/app_code.txt}
                if [ -f "$PHALA_APP_CODE_PATH" ]; then
                    EXPECTED_COMPOSE_HASH=$(python3 -c 'import hashlib,json,sys
text=open(sys.argv[1], encoding="utf-8").read()
start=text.index("{")
marker="\n\nis_registered"
end=text.index(marker) if marker in text else text.rindex("}") + 1
obj=json.loads(text[start:end])
print(hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest())' "$PHALA_APP_CODE_PATH")
                else
                    EXPECTED_COMPOSE_HASH=$(sha256sum "$PHALA_COMPOSE_PATH" | awk '{print $1}')
                fi
                echo "Recording expected Phala compose policy hash on TDX verifier: sha256:$EXPECTED_COMPOSE_HASH"
                if [ "$TDX_V4_CONTRACT_VARIANT" = "full" ]; then
                    run_cast_send_logged "smart_contracts_init" "set_expected_compose_hash" "$OWNER" "" \
                        --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                        $DCAP_TDX_V4_ADDRESS "setExpectedComposeHash(bytes32)" "0x$EXPECTED_COMPOSE_HASH"
                else
                    run_cast_send_logged "smart_contracts_init" "set_expected_compose_hash" "$OWNER" "" \
                        --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                        $RTMR3_REPLAY_POLICY_ADDRESS "setExpectedComposeHash(bytes32)" "0x$EXPECTED_COMPOSE_HASH"
                fi
            elif [ "$TDX_V4_CONTRACT_VARIANT" != "full" ]; then
                echo "Skipping compose-hash policy wiring for lightweight TDX V4 variant"
            fi
            if [ "$TDX_V4_CONTRACT_VARIANT" = "full" ]; then
                PHALA_RTMR3_EVENT_LOG_PATH=${PHALA_RTMR3_EVENT_LOG_PATH:-../phala/rtmr3_event_log.txt}
                if [ -f "$PHALA_RTMR3_EVENT_LOG_PATH" ]; then
                    EXPECTED_COMPOSE_EVENT_DIGEST=$(python3 -c 'import json,re,sys
text=open(sys.argv[1], encoding="utf-8").read()
for match in re.finditer(r"\{[^{}]*\}", text, flags=re.S):
    try:
        obj=json.loads(match.group(0))
    except json.JSONDecodeError:
        continue
    if obj.get("imr") == 3 and obj.get("event") == "compose-hash":
        print(obj["digest"])
        break' "$PHALA_RTMR3_EVENT_LOG_PATH")
                    if [ -n "$EXPECTED_COMPOSE_EVENT_DIGEST" ]; then
                        echo "Recording expected Phala compose RTMR3 event digest on TDX verifier: sha384:$EXPECTED_COMPOSE_EVENT_DIGEST"
                        run_cast_send_logged "smart_contracts_init" "set_expected_compose_event_digest" "$OWNER" "" \
                            --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                            $DCAP_TDX_V4_ADDRESS "setExpectedComposeEventDigest(bytes)" "0x$EXPECTED_COMPOSE_EVENT_DIGEST"
                    fi
                fi
            else
                if [ -f "${PHALA_RTMR3_EVENT_LOG_PATH:-../phala/rtmr3_event_log.txt}" ]; then
                    PHALA_RTMR3_EVENT_LOG_PATH=${PHALA_RTMR3_EVENT_LOG_PATH:-../phala/rtmr3_event_log.txt}
                    EXPECTED_COMPOSE_EVENT_DIGEST=$(python3 -c 'import json,re,sys
text=open(sys.argv[1], encoding="utf-8").read()
for match in re.finditer(r"\{[^{}]*\}", text, flags=re.S):
    try:
        obj=json.loads(match.group(0))
    except json.JSONDecodeError:
        continue
    if obj.get("imr") == 3 and obj.get("event") == "compose-hash":
        print(obj["digest"])
        break' "$PHALA_RTMR3_EVENT_LOG_PATH")
                    if [ -n "$EXPECTED_COMPOSE_EVENT_DIGEST" ]; then
                        echo "Recording expected Phala compose RTMR3 event digest on replay policy: sha384:$EXPECTED_COMPOSE_EVENT_DIGEST"
                        run_cast_send_logged "smart_contracts_init" "set_expected_compose_event_digest" "$OWNER" "" \
                            --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                            $RTMR3_REPLAY_POLICY_ADDRESS "setExpectedComposeEventDigest(bytes)" "0x$EXPECTED_COMPOSE_EVENT_DIGEST"
                    fi
                fi
            fi
            authorize_pccs_reader "$DCAP_TDX_V4_ADDRESS"
            run_cast_send_logged "smart_contracts_init" "set_tdx_v4_attestation" "$OWNER" "" \
                --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                $DEVICE_REGISTRY_ADDRESS "setTdxV4Attestation(address)" $DCAP_TDX_V4_ADDRESS
	    fi

	    UPLOAD_PCCS_COLLATERALS=${UPLOAD_PCCS_COLLATERALS:-1}
	    if [ "$UPLOAD_PCCS_COLLATERALS" = "1" ]; then
        PCCS_QUOTE_PATH=${PCCS_QUOTE_PATH:-../data/phala_tdx_quote}
        PCCS_TMP_DIR=${PCCS_TMP_DIR:-./data/pccs_temp}
        mkdir -p "$PCCS_TMP_DIR"
        echo "PCCS_TMP_DIR: $PCCS_TMP_DIR"
        if [ -z "${PCCS_FMSPC:-}" ] && [ -f "$PCCS_QUOTE_PATH" ]; then
            PCCS_TMP_DIR=${PCCS_TMP_DIR:-$(mktemp -d)}
            PCCS_PCK_CERT="$PCCS_TMP_DIR/pck_cert.pem"

            PYTHON_BIN=${PYTHON_BIN:-}
            if [ -z "$PYTHON_BIN" ]; then
                if command -v python3 >/dev/null 2>&1; then
                    PYTHON_BIN=python3
                elif command -v python >/dev/null 2>&1; then
                    PYTHON_BIN=python
                fi
            fi

            if [ -z "$PYTHON_BIN" ]; then
                echo "python3/python not found; skipping PCCS_FMSPC extraction"
            else
                "$PYTHON_BIN" - <<'PY' "$PCCS_QUOTE_PATH" "$PCCS_PCK_CERT"
import sys

quote_path = sys.argv[1]
out_cert = sys.argv[2]

data = open(quote_path, "rb").read()
text = data.strip()

def is_hex_blob(blob: bytes) -> bool:
    if not blob:
        return False
    for b in blob:
        if b not in b"0123456789abcdefABCDEF\n\r \t":
            return False
    return True

if is_hex_blob(text):
    hex_str = b"".join(text.split())
    data = bytes.fromhex(hex_str.decode("ascii"))

begin = b"-----BEGIN CERTIFICATE-----"
end = b"-----END CERTIFICATE-----"
start = data.find(begin)
if start == -1:
    sys.exit(1)
stop = data.find(end, start)
if stop == -1:
    sys.exit(1)

pem = data[start:stop + len(end)] + b"\n"
with open(out_cert, "wb") as f:
    f.write(pem)
PY
            fi

            if [ -f "$PCCS_PCK_CERT" ]; then
                PCCS_FMSPC=$(openssl asn1parse -in "$PCCS_PCK_CERT" -i -dump | awk '
/1\.2\.840\.113741\.1\.13\.1\.4/ {found=1; next}
found && /HEX DUMP/ {sub(/^.*HEX DUMP:/,"", $0); gsub(/[^0-9A-Fa-f]/, "", $0); print; exit}
')
                if [ -n "$PCCS_FMSPC" ]; then
                    export PCCS_FMSPC
                    echo "PCCS_FMSPC extracted: $PCCS_FMSPC"
                fi
            fi
        fi

        PCCS_FETCH=${PCCS_FETCH:-1}
        if [ "$PCCS_FETCH" = "1" ]; then
            PCCS_BASE_URL=${PCCS_BASE_URL:-https://pccs.phala.network}
            PCCS_TEE=${PCCS_TEE:-tdx}
            if [ -n "${PCCS_FMSPC:-}" ]; then
                PCCS_TMP_DIR=${PCCS_TMP_DIR:-./data/pccs_temp}
                mkdir -p "$PCCS_TMP_DIR"
                echo "PCCS_TMP_DIR: $PCCS_TMP_DIR"
                PCCS_IDENTITY_JSON_URL="$PCCS_BASE_URL/$PCCS_TEE/certification/v4/qe/identity"
                PCCS_TCBINFO_JSON_URL="$PCCS_BASE_URL/$PCCS_TEE/certification/v4/tcb?fmspc=$PCCS_FMSPC"

                if ! curl -fsSL "$PCCS_IDENTITY_JSON_URL" -o "$PCCS_TMP_DIR/qe_identity.json"; then
                    echo "Failed to fetch PCCS identity JSON from $PCCS_IDENTITY_JSON_URL"
                    exit 1
                fi

                if ! curl -fsSL "$PCCS_TCBINFO_JSON_URL" -o "$PCCS_TMP_DIR/tcb.json"; then
                    echo "Failed to fetch PCCS TCBInfo JSON from $PCCS_TCBINFO_JSON_URL"
                    exit 1
                fi

                export PCCS_IDENTITY_JSON="$(cat "$PCCS_TMP_DIR/qe_identity.json")"
                export PCCS_TCBINFO_JSON="$(cat "$PCCS_TMP_DIR/tcb.json")"
                if [ "$PCCS_TEE" = "tdx" ]; then
                    export PCCS_QUOTE_VERSION=4
                fi

	                encode_base64() {
	                    if command -v xxd >/dev/null 2>&1; then
	                        printf '0x'
	                        xxd -p -c 999999 "$1" | tr -d '\n'
	                    elif command -v od >/dev/null 2>&1; then
	                        printf '0x'
	                        od -An -v -tx1 "$1" | tr -d ' \n'
	                    else
	                        echo "Neither xxd nor od is available for hex encoding" >&2
	                        return 1
	                    fi
	                }

	                normalize_binary_file() {
	                    local target_file=$1
	                    "$PYTHON_BIN" - <<'PY' "$target_file"
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = path.read_bytes()
trimmed = data.strip()

if trimmed and all(b in b"0123456789abcdefABCDEF\r\n\t " for b in trimmed):
    hex_bytes = b"".join(trimmed.split())
    if len(hex_bytes) % 2 != 0:
        raise SystemExit(f"hex payload in {path} has odd length")
    path.write_bytes(bytes.fromhex(hex_bytes.decode("ascii")))
PY
	                }

                PYTHON_BIN=${PYTHON_BIN:-}
                if [ -z "$PYTHON_BIN" ]; then
                    if command -v python3 >/dev/null 2>&1; then
                        PYTHON_BIN=python3
                    elif command -v python >/dev/null 2>&1; then
                        PYTHON_BIN=python
                    fi
                fi
                if [ -z "$PYTHON_BIN" ]; then
                    echo "python3/python not found; skipping PCCS cert/CRL fetch"
                else
                    TCB_HEADERS="$PCCS_TMP_DIR/tcb.headers"
                    PCK_HEADERS="$PCCS_TMP_DIR/pck.headers"

                    curl -sS -D "$TCB_HEADERS" "$PCCS_TCBINFO_JSON_URL" -o /dev/null
                    TCB_CHAIN=$(grep -i "^tcb-info-issuer-chain:" "$TCB_HEADERS" | sed "s/^[^:]*: //")
                    if [ -z "$TCB_CHAIN" ]; then
                        echo "Failed to read tcb-info-issuer-chain from PCCS headers"
                        exit 1
                    fi

                    printf '%s' "$TCB_CHAIN" | "$PYTHON_BIN" -c 'import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read()))' \
                        > "$PCCS_TMP_DIR/tcb_chain.pem"
                    awk '/BEGIN CERTIFICATE/{i++} {print > ("'$PCCS_TMP_DIR'/tcb_cert_" i ".pem")}' \
                         "$PCCS_TMP_DIR/tcb_chain.pem"
                    openssl x509 -in "$PCCS_TMP_DIR/tcb_cert_1.pem" -outform der -out "$PCCS_TMP_DIR/tcb_signing.der"
                    openssl x509 -in "$PCCS_TMP_DIR/tcb_cert_2.pem" -outform der -out "$PCCS_TMP_DIR/root_ca.der"

	                    curl -sS -D "$PCK_HEADERS" "$PCCS_BASE_URL/sgx/certification/v4/pckcrl?ca=platform&encoding=der" \
	                        -o "$PCCS_TMP_DIR/pckcrl.der"
	                    normalize_binary_file "$PCCS_TMP_DIR/pckcrl.der"
	                    PCK_CHAIN=$(grep -i "^sgx-pck-crl-issuer-chain:" "$PCK_HEADERS" | sed "s/^[^:]*: //")
	                    if [ -z "$PCK_CHAIN" ]; then
	                        echo "Failed to read sgx-pck-crl-issuer-chain from PCCS headers"
	                        exit 1
                    fi

                    printf '%s' "$PCK_CHAIN" | "$PYTHON_BIN" -c 'import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read()))' \
                        > "$PCCS_TMP_DIR/pck_chain.pem"
                    awk '/BEGIN CERTIFICATE/{i++} {print > ("'$PCCS_TMP_DIR'/pck_cert_" i ".pem")}' \
                         "$PCCS_TMP_DIR/pck_chain.pem"
                    openssl x509 -in "$PCCS_TMP_DIR/pck_cert_1.pem" -outform der -out "$PCCS_TMP_DIR/platform_ca.der"

	                    curl -sS "$PCCS_BASE_URL/sgx/certification/v4/rootcacrl?encoding=der" \
	                        -o "$PCCS_TMP_DIR/rootcacrl.der"
	                    normalize_binary_file "$PCCS_TMP_DIR/rootcacrl.der"

	                    export PCCS_TCB_SIGNING_PATH="$PCCS_TMP_DIR/tcb_signing.der"
	                    export PCCS_ROOT_CA_PATH="$PCCS_TMP_DIR/root_ca.der"
	                    export PCCS_PLATFORM_CA_PATH="$PCCS_TMP_DIR/platform_ca.der"
	                    export PCCS_PLATFORM_CRL_PATH="$PCCS_TMP_DIR/pckcrl.der"
	                    export PCCS_ROOT_CRL_PATH="$PCCS_TMP_DIR/rootcacrl.der"
	                    if ! export PCCS_TCB_SIGNING_DER="$(encode_base64 "$PCCS_TMP_DIR/tcb_signing.der")"; then
	                        echo "Failed to encode $PCCS_TMP_DIR/tcb_signing.der"
	                        exit 1
	                    fi
	                    if ! export PCCS_ROOT_CA_DER="$(encode_base64 "$PCCS_TMP_DIR/root_ca.der")"; then
	                        echo "Failed to encode $PCCS_TMP_DIR/root_ca.der"
	                        exit 1
	                    fi
	                    if ! export PCCS_PLATFORM_CA_DER="$(encode_base64 "$PCCS_TMP_DIR/platform_ca.der")"; then
	                        echo "Failed to encode $PCCS_TMP_DIR/platform_ca.der"
	                        exit 1
	                    fi
	                    if ! export PCCS_PLATFORM_CRL_DER="$(encode_base64 "$PCCS_TMP_DIR/pckcrl.der")"; then
	                        echo "Failed to encode $PCCS_TMP_DIR/pckcrl.der"
	                        exit 1
	                    fi
	                    if ! export PCCS_ROOT_CRL_DER="$(encode_base64 "$PCCS_TMP_DIR/rootcacrl.der")"; then
	                        echo "Failed to encode $PCCS_TMP_DIR/rootcacrl.der"
	                        exit 1
	                    fi
	                    echo "PCCS certificates prepared in $PCCS_TMP_DIR"
                    echo "PCCS_TCB_SIGNING_DER prepared"
                    echo "PCCS_ROOT_CA_DER prepared"
                    echo "PCCS_PLATFORM_CA_DER prepared"
                    echo "PCCS_PLATFORM_CRL_DER prepared"
                    echo "PCCS_ROOT_CRL_DER prepared"
                fi
            if [ "$BLOCKCHAIN_PROVIDER" = "anvil" ]; then
                cast rpc --rpc-url $rpc_url evm_mine
            else
                echo "Skipping evm_mine for BLOCKCHAIN_PROVIDER=$BLOCKCHAIN_PROVIDER"
            fi
            fi
        fi

            pccs_upload_log=$(mktemp)
            if ! forge script script/UploadPccsCollaterals.s.sol --broadcast "${FORGE_REMOTE_FLAGS[@]}" --rpc-url $rpc_url --ffi \
                >"$pccs_upload_log" 2>&1; then
                cat "$pccs_upload_log"
                if grep -q "Duplicate_Collateral()" "$pccs_upload_log"; then
                    echo "PCCS collaterals already present on-chain; continuing without re-upload"
                else
                    rm -f "$pccs_upload_log"
                    echo "PCCS collateral upload failed"
                    exit 1
                fi
            else
                cat "$pccs_upload_log"
                log_broadcast_gas_cost "upload_pccs_collaterals" "./broadcast/UploadPccsCollaterals.s.sol/$CHAIN_ID/run-latest.json"
                echo "PCCS Collaterals uploaded successfully"
            fi
            rm -f "$pccs_upload_log"

	        # Hardcoded startup quote verification is disabled here on purpose.
	        # Runtime TEE quotes should be supplied and verified by the worker flow.
	        # VERIFY_TDX_QUOTE_ONCHAIN=${VERIFY_TDX_QUOTE_ONCHAIN:-0}
	        # if [ "$VERIFY_TDX_QUOTE_ONCHAIN" = "1" ] && [ -n "${DCAP_TDX_V4_ADDRESS:-}" ]; then
	        #     export QUOTE_PATH="$PCCS_QUOTE_PATH"
	        #     if ! forge script script/VerifyTDXV4Quote.s.sol --rpc-url $rpc_url; then
	        #         echo "TDX V4 quote verification failed"
	        #         exit 1
	        #     fi
	        # fi
	    fi
fi

PRIMARY_WORKER_ADDRESS=${W0_ACCOUNT_ADDRESS:-}
PRIMARY_WORKER_PRIVATE_KEY=${W0_PRIVATE_KEY:-}
SECONDARY_WORKER_ADDRESS=${W1_ACCOUNT_ADDRESS:-}
TERTIARY_WORKER_ADDRESS=${W2_ACCOUNT_ADDRESS:-}

require_address W0_ACCOUNT_ADDRESS "$PRIMARY_WORKER_ADDRESS"
require_private_key W0_PRIVATE_KEY "$PRIMARY_WORKER_PRIVATE_KEY"
if [ -n "$SECONDARY_WORKER_ADDRESS" ]; then
    require_address W1_ACCOUNT_ADDRESS "$SECONDARY_WORKER_ADDRESS"
fi
if [ -n "$TERTIARY_WORKER_ADDRESS" ]; then
    require_address W2_ACCOUNT_ADDRESS "$TERTIARY_WORKER_ADDRESS"
fi

print_authorization_status() {
    local address=$1
    local expected="0x$(printf '%063d1')"
    local response

    response=$(cast call --rpc-url "$rpc_url" "$DEVICE_REGISTRY_ADDRESS" "isAuthorized(address)" "$address" 2>/dev/null || true)
    if [ -z "$response" ]; then
        echo "$address: unknown (authorization read failed)"
        return 0
    fi

    if [ "$response" = "$expected" ]; then
        echo "$address: yes"
    else
        echo "$address: no"
    fi
}

run_cast_send_logged "smart_contracts_init" "set_current_aggregator" "$DEPLOYER_ADDRESS" "" \
    --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
    $AGGREGATOR_SELECTION_ADDRESS "setCurrentAggregator(address)" "$PRIMARY_WORKER_ADDRESS"

echo "========= node server environment variables ========="
echo "REGISTRY_ADDRESS= $DEVICE_REGISTRY_ADDRESS"
echo "AGGREGATOR_ADDRESS= $AGGREGATOR_SELECTION_ADDRESS"
echo "GM_STORAGE_ADDRESS= $GMSTORAGE"
echo "ACCOUNT_ADDRESS= $PRIMARY_WORKER_ADDRESS"
echo "SEPOLIA_RPC_URL= $rpc_url"
echo "AUTOMATA_DCAP_V3_ ATTESTATION_URL= ${DCAP_ADDRESS:-}"
echo "AUTOMATA_DCAP_TDX_V4_ATTESTATION_URL= ${DCAP_TDX_V4_ADDRESS:-}"
echo "===================================================="

run_cast_send_logged "smart_contracts_init" "set_gm_storage_address" "$PRIMARY_WORKER_ADDRESS" "$W0_DEVICE_ID" \
    --rpc-url $rpc_url --private-key $PRIMARY_WORKER_PRIVATE_KEY \
    $AGGREGATOR_SELECTION_ADDRESS "setGMStorageAddress(address)" $GMSTORAGE

echo "GMStorage Addresse wurde in AggregatorSelection gesetzt"

AGGREGATOR_TIMEOUT_REPORT_PERCENT=${AGGREGATOR_TIMEOUT_REPORT_PERCENT:-50}
run_cast_send_logged "smart_contracts_init" "set_timeout_report_threshold_percent" "$PRIMARY_WORKER_ADDRESS" "$W0_DEVICE_ID" \
    --rpc-url $rpc_url --private-key $PRIMARY_WORKER_PRIVATE_KEY \
    $AGGREGATOR_SELECTION_ADDRESS "setTimeoutReportThresholdPercent(uint256)" "$AGGREGATOR_TIMEOUT_REPORT_PERCENT"

echo "Aggregator timeout report threshold wurde auf ${AGGREGATOR_TIMEOUT_REPORT_PERCENT}% gesetzt"

echo "Authorization Status:"
print_authorization_status "$PRIMARY_WORKER_ADDRESS"
if [ -n "$SECONDARY_WORKER_ADDRESS" ]; then
    print_authorization_status "$SECONDARY_WORKER_ADDRESS"
fi
if [ -n "$TERTIARY_WORKER_ADDRESS" ]; then
    print_authorization_status "$TERTIARY_WORKER_ADDRESS"
fi

if [ "${DOCKER:-}" = "phala" ]; then
    if [ "$IPFS_PROVIDER" = "kubo" ]; then
        echo "Pinning the initial GM to the IPFS node"
        curl --connect-timeout 5 --max-time 60 -sSf -X POST "https://61ecc557e3b36593390057d322d46e9488032c34-5001.dstack-prod5.phala.network/api/v0/pin/add?arg=${INITIAL_GM_CID}"
        curl --connect-timeout 5 --max-time 60 -sSf -X POST "https://61ecc557e3b36593390057d322d46e9488032c34-5001.dstack-prod5.phala.network/api/v0/files/cp?arg=/ipfs/${INITIAL_GM_CID}&arg=/start"
    fi
else
    if [ "$IPFS_PROVIDER" = "kubo" ]; then
        echo "Copying the locally imported initial GM into IPFS MFS"
        curl --connect-timeout 5 --max-time 15 -sSf -X POST \
            "${KUBO_API_URL}/api/v0/files/rm?arg=/start&force=true" >/dev/null || true
        curl --connect-timeout 5 --max-time 15 -sSf -X POST \
            "${KUBO_API_URL}/api/v0/files/rm?arg=/start.sig&force=true" >/dev/null || true
        curl --connect-timeout 5 --max-time 15 -sSf -X POST \
            "${KUBO_API_URL}/api/v0/files/cp?arg=/ipfs/${INITIAL_GM_CID}&arg=/start&parents=true"
        curl --connect-timeout 5 --max-time 15 -sSf -X POST \
            "${KUBO_API_URL}/api/v0/files/cp?arg=/ipfs/${INITIAL_GM_SIG_CID}&arg=/start.sig&parents=true"
    fi
fi

prepare_encrypted_initial_gm

KEEP_ALIVE=${KEEP_ALIVE:-0}
if [ "$KEEP_ALIVE" = "1" ]; then
    echo "KEEP_ALIVE=1: keeping container running"
    tail -f /dev/null
fi

echo "starter_docker.sh completed; exiting because KEEP_ALIVE=${KEEP_ALIVE}"
exit 0

### Signature und public key registration

#RISC0_DEV_MODE=1 cargo run -- --chain-id 31337 --eth-wallet-private-key $PRIVATE_KEY_0 --rpc-url $rpc_url --contract $DEVICE_REGISTRY_ADDRESS
#RISC0_DEV_MODE=0 cargo run -- --chain-id 31337 --eth-wallet-private-key $PRIVATE_KEY_0 --rpc-url $rpc_url --contract $DEVICE_REGISTRY_ADDRESS
