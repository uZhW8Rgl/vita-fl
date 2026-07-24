#!/bin/bash

set -euo pipefail


: "${ETH_WALLET_PRIVATE_KEY:?ETH_WALLET_PRIVATE_KEY must be supplied through the runtime environment}"

echo "Wallet signer configured from protected runtime input."

#cargo clean

#cargo build

if [ -n "${RPC_URL:-}" ]; then
    export rpc_url="$RPC_URL"
elif [ "${DOCKER:-}" = "phala" ]; then
    export rpc_url=https://61ecc557e3b36593390057d322d46e9488032c34-8545.dstack-prod5.phala.network
else
    export rpc_url=http://anvil:8545
fi
export RPC_URL=$rpc_url
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
KUBO_API_URL=${KUBO_API_URL:-http://ipfs:5001}
IPFS_PROVIDER=${IPFS_PROVIDER:-kubo}

materialize_runtime_key() {
    local env_name=$1
    local output_path=$2
    local mode=$3
    local value=${!env_name:-}

    if [ -z "$value" ]; then
        return 0
    fi

    mkdir -p "$(dirname "$output_path")"
    value=${value//\\n/$'\n'}
    (umask 077; printf '%s\n' "$value" >"$output_path")
    chmod "$mode" "$output_path"
}

if [ -n "${INITIAL_GM_SIGNING_KEY:-}" ]; then
    export INITIAL_GM_SIGNING_KEY_PATH=/run/dfl-secrets/initial-gm-signing-key.pem
    materialize_runtime_key INITIAL_GM_SIGNING_KEY "$INITIAL_GM_SIGNING_KEY_PATH" 600
fi

if [ -n "${INITIAL_BOOTSTRAP_RECIPIENT_PUBLIC_KEY:-}" ]; then
    export INITIAL_BOOTSTRAP_RECIPIENT_PUBLIC_KEY_PATH=/run/dfl-secrets/bootstrap-recipient-public-key.pem
    materialize_runtime_key INITIAL_BOOTSTRAP_RECIPIENT_PUBLIC_KEY "$INITIAL_BOOTSTRAP_RECIPIENT_PUBLIC_KEY_PATH" 644
fi

using_local_runtime_services() {
    [[ "$rpc_url" == "http://anvil:8545" || "$rpc_url" == "http://127.0.0.1:8545" ]] && \
    [[ "$KUBO_API_URL" == "http://ipfs:5001" || "$KUBO_API_URL" == "http://127.0.0.1:5001" ]]
}

wait_for_anvil() {
    local attempts=${1:-60}
    local rpc_endpoint=$rpc_url

    echo "Waiting for Anvil at ${rpc_endpoint}..."
    for _ in $(seq 1 "$attempts"); do
        if cast chain-id --rpc-url "$rpc_endpoint" >/dev/null 2>&1; then
            echo "Anvil is ready."
            return 0
        fi
        sleep 2
    done

    echo "Timed out waiting for Anvil at ${rpc_endpoint}."
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

clear_runtime_ready_marker() {
    if [ "$IPFS_PROVIDER" != "kubo" ]; then
        return 0
    fi

    echo "Clearing stale runtime ready marker from Kubo MFS: /runtime/ready.json"
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/rm?arg=/runtime/ready.json&force=true" >/dev/null || true
}

fund_configured_worker_accounts() {
    if ! using_local_runtime_services; then
        return 0
    fi
    if [ "${FUND_WORKER_ACCOUNTS:-1}" != "1" ]; then
        echo "Skipping worker-account funding; the configured Anvil accounts are already funded."
        return 0
    fi

    local balance_wei=${WORKER_ACCOUNT_BALANCE_WEI:-10000000000000000000000}
    local balance_hex
    balance_hex=$(cast to-hex "$balance_wei")
    local addresses_csv=${WORKER_ACCOUNT_ADDRESSES:-}
    local addresses=()
    local address
    local index

    if [ -n "$addresses_csv" ]; then
        IFS=',' read -ra addresses <<< "$addresses_csv"
    fi

    for index in $(seq 0 $((${WORKER_COUNT:-20} - 1))); do
        local env_name="W${index}_ACCOUNT_ADDRESS"
        local env_value=${!env_name:-}
        if [ -n "$env_value" ]; then
            addresses+=("$env_value")
        fi
    done

    if [ "${#addresses[@]}" -eq 0 ]; then
        echo "No worker accounts configured for Anvil funding."
        return 0
    fi

    echo "Funding configured worker accounts on local Anvil."
    local funded_addresses=" "
    for address in "${addresses[@]}"; do
        address=$(printf '%s' "$address" | xargs)
        if [ -z "$address" ]; then
            continue
        fi
        require_address WORKER_ACCOUNT_ADDRESS "$address"
        if [[ "$funded_addresses" == *" $address "* ]]; then
            continue
        fi
        funded_addresses+="$address "
        cast rpc --rpc-url "$rpc_url" anvil_setBalance "$address" "$balance_hex" >/dev/null
        echo "Funded worker account $address with $balance_wei wei"
    done
}

wait_for_anvil
wait_for_kubo
clear_runtime_ready_marker

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

fund_configured_worker_accounts

publish_runtime_contract_manifest() {
    if [ "$IPFS_PROVIDER" != "kubo" ]; then
        return 0
    fi

    require_address DEVICE_REGISTRY_ADDRESS "$DEVICE_REGISTRY_ADDRESS"
    require_address AGGREGATOR_SELECTION_ADDRESS "$AGGREGATOR_SELECTION_ADDRESS"
    require_address GMSTORAGE "$GMSTORAGE"
    require_address MEDICAL_SIGNER_REGISTRY_ADDRESS "$MEDICAL_SIGNER_REGISTRY_ADDRESS"

    local manifest_file
    manifest_file=$(mktemp)
    cat >"$manifest_file" <<EOF
{"registry_address":"$DEVICE_REGISTRY_ADDRESS","aggregator_address":"$AGGREGATOR_SELECTION_ADDRESS","gm_storage_address":"$GMSTORAGE","medical_signer_registry_address":"$MEDICAL_SIGNER_REGISTRY_ADDRESS","rpc_url":"$rpc_url","chain_id":"$CHAIN_ID"}
EOF

    echo "Publishing runtime contract manifest to Kubo MFS: /runtime/contracts.json"
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/mkdir?arg=/runtime&parents=true" >/dev/null || true
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        -F "file=@${manifest_file}" \
        "${KUBO_API_URL}/api/v0/files/write?arg=/runtime/contracts.json&create=true&truncate=true&parents=true" >/dev/null
    rm -f "$manifest_file"
}

publish_runtime_ready_marker() {
    if [ "$IPFS_PROVIDER" != "kubo" ]; then
        return 0
    fi

    local ready_file
    ready_file=$(mktemp)
    cat >"$ready_file" <<EOF
{"status":"ready","chain_id":"$CHAIN_ID","rpc_url":"$rpc_url","timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

    echo "Publishing runtime ready marker to Kubo MFS: /runtime/ready.json"
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/mkdir?arg=/runtime&parents=true" >/dev/null || true
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        -F "file=@${ready_file}" \
        "${KUBO_API_URL}/api/v0/files/write?arg=/runtime/ready.json&create=true&truncate=true&parents=true" >/dev/null
    rm -f "$ready_file"
}

extract_worker_image_digest() {
    local image_ref=${EXPECTED_WORKER_IMAGE:-}

    if [ -z "$image_ref" ]; then
        return 0
    fi

    printf '%s' "$image_ref" | sed -nE 's/^.+@sha256:([0-9a-fA-F]{64})$/\1/p'
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

prepare_local_initial_gm() {
    if ! using_local_runtime_services; then
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
    INITIAL_GM_CID=$(add_file_to_kubo "$model_path")
    INITIAL_GM_SIG_CID=$(add_file_to_kubo "$signature_path")

    echo "Local initial GM CID: $INITIAL_GM_CID"
    echo "Local initial GM signature CID: $INITIAL_GM_SIG_CID"
}

prepare_encrypted_initial_gm() {
    if ! using_local_runtime_services; then
        return 0
    fi

    if [ "$IPFS_PROVIDER" = "pinata" ]; then
        echo "Encrypted bootstrap initialization is not implemented for IPFS_PROVIDER=pinata"
        echo "Use IPFS_PROVIDER=kubo or add an equivalent Pinata upload-and-initialize path"
        exit 1
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
    if [ ! -f "$signature_path" ]; then
        echo "Missing initial GM signature file for encrypted bootstrap: $signature_path"
        exit 1
    fi
    if [ ! -f "$signing_key_path" ]; then
        echo "Missing initial GM signing key for encrypted bootstrap: $signing_key_path"
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
    local recipient_count
    local publisher_public_key_der_hex
    bundle_path=$(printf '%s' "$bootstrap_json" | jq -re '.bundlePath')
    bundle_signature_path=$(printf '%s' "$bootstrap_json" | jq -re '.bundleSignaturePath')
    key_bundle_path=$(printf '%s' "$bootstrap_json" | jq -re '.keyBundlePath')
    recipient_count=$(printf '%s' "$bootstrap_json" | jq -re '.recipientCount')
    publisher_public_key_der_hex=$(
        printf '%s' "$bootstrap_json" |
            jq -re '.publisherPublicKeyDerHex | select(test("^[0-9a-f]+$") and ((length % 2) == 0))'
    )

    echo "Encrypted bootstrap recipients from registry, preprovisioned inventory, and fallback: $recipient_count"

    local encrypted_model_cid
    local encrypted_sig_cid
    local encrypted_key_cid
    encrypted_model_cid=$(add_file_to_kubo "$bundle_path")
    encrypted_sig_cid=$(add_file_to_kubo "$bundle_signature_path")
    encrypted_key_cid=$(add_file_to_kubo "$key_bundle_path")

    echo "Encrypted initial GM bundle CID: $encrypted_model_cid"
    echo "Encrypted initial GM signature CID: $encrypted_sig_cid"
    echo "Encrypted initial GM key bundle CID: $encrypted_key_cid"

    cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
        $GMSTORAGE "initializeEncryptedBootstrap(string,string,string,bytes)" \
        "$encrypted_model_cid" "$encrypted_sig_cid" "$encrypted_key_cid" \
        "0x${publisher_public_key_der_hex}"
}

prepare_local_initial_gm
forge script --rpc-url $rpc_url --broadcast script/Deploy.s.sol
log_broadcast_gas_cost "deploy_core_contracts" "./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json"

export DEVICE_REGISTRY_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "DeviceRegistry") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export AGGREGATOR_SELECTION_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "AggregatorSelection") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export GMSTORAGE=$(jq -re '.transactions[] | select(.contractName == "GMStorage") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export MEDICAL_SIGNER_REGISTRY_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "MedicalSignerRegistry") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

ENABLE_DCAP=${ENABLE_DCAP:-1}
if [ "$ENABLE_DCAP" = "1" ]; then
    echo "Deploying Automata DCAP v4 contracts"

    export PRIVATE_KEY=$ETH_WALLET_PRIVATE_KEY
    export DCAP_IMAGE_ID=${DCAP_IMAGE_ID:-0x97f41badbcc8d79521f10cd076fa7a2ed67b84abe07c496da11a2a708c9e5f14}

    if using_local_runtime_services; then
        FALLBACK_P256_VERIFIER_ADDRESS=0xc2b78104907F722DABAc4C69f826a522B2754De4
        P256_SOURCE_PATH="lib/p256-verifier/src/P256Verifier.sol"
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

        NATIVE_P256_VERIFIER_ADDRESS=0x0000000000000000000000000000000000000100
        P256_PROBE_INPUT=0xbb5a52f42f9c9261ed4361f59422a1e30036e7c32b270c8807a419feca6050232ba3a8be6b94d5ec80a6d9d1190a436effe50d85a1eee859b8cc6af9bd5c2e184cd60b855d442f5b3c7b11eb6c4e0ae7525fe710fab9aa7c77a67f79e6fadd762927b10512bae3eddcfe467828128bad2903269919f7086069c8c4df6c732838c7787964eaac00e5921fb1498a60f4606766b3d9685001558d1a974e7341513e
        P256_PROBE_EXPECTED=0x0000000000000000000000000000000000000000000000000000000000000001
        install_p256_verifier() {
            local verifier=$1
            cast rpc --rpc-url $rpc_url anvil_setCode "$verifier" "$P256_RUNTIME"
        }

        probe_p256_route() {
            local verifier=$1
            local output
            output=$(cast call --rpc-url "$rpc_url" "$verifier" --data "$P256_PROBE_INPUT" 2>/dev/null || true)
            if [ "$output" != "$P256_PROBE_EXPECTED" ]; then
                printf '%s\n' "unavailable"
            elif [ "$verifier" = "$NATIVE_P256_VERIFIER_ADDRESS" ]; then
                printf '%s\n' "native-precompile"
            elif [ "$verifier" = "$FALLBACK_P256_VERIFIER_ADDRESS" ]; then
                printf '%s\n' "fallback-contract"
            else
                printf '%s\n' "configured-address"
            fi
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

    export OWNER=$(cast wallet address --private-key "$ETH_WALLET_PRIVATE_KEY")
    require_address OWNER "$OWNER"

    forge script script/helper/DeployHelpers.s.sol --sig "deployEnclaveIdentityHelper()" --broadcast --rpc-url $rpc_url --ffi
    log_broadcast_gas_cost "deploy_enclave_identity_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployEnclaveIdentityHelper-latest.json"
    export ENCLAVE_IDENTITY_HELPER=$(jq -re '.transactions[] | select(.contractName == "EnclaveIdentityHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployEnclaveIdentityHelper-latest.json)

    forge script script/helper/DeployHelpers.s.sol --sig "deployFmspcTcbHelper()" --broadcast --rpc-url $rpc_url
    log_broadcast_gas_cost "deploy_fmspc_tcb_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployFmspcTcbHelper-latest.json"
    export FMSPC_TCB_HELPER=$(jq -re '.transactions[] | select(.contractName == "FmspcTcbHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployFmspcTcbHelper-latest.json)

    forge script script/helper/DeployHelpers.s.sol --sig "deployPckHelper()" --broadcast --rpc-url $rpc_url
    log_broadcast_gas_cost "deploy_pck_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployPckHelper-latest.json"
    export X509_HELPER=$(jq -re '.transactions[] | select(.contractName == "PCKHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployPckHelper-latest.json)

    forge script script/helper/DeployHelpers.s.sol --sig "deployX509CrlHelper()" --broadcast --rpc-url $rpc_url
    log_broadcast_gas_cost "deploy_x509_crl_helper" "./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployX509CrlHelper-latest.json"
    export X509_CRL_HELPER=$(jq -re '.transactions[] | select(.contractName == "X509CRLHelper") | .contractAddress' ./broadcast/DeployHelpers.s.sol/$CHAIN_ID/deployX509CrlHelper-latest.json)

    PCCS_DEPLOYMENT_FILE="./deployment/$CHAIN_ID.json"

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
	        TDX_CREATE_JSON=$(forge create src/attestation/AutomataDcapTdxV4Attestation.sol:AutomataDcapTdxV4Attestation \
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
		        DSTACK_REFERENCE_QUOTE_PATH=${DSTACK_REFERENCE_QUOTE_PATH:-${TDX_REFERENCE_QUOTE_PATH:-${PCCS_QUOTE_PATH:-../data/phala_tdx_quote}}}
		        if [ ! -f "$DSTACK_REFERENCE_QUOTE_PATH" ]; then
		            echo "A dstack reference quote is required to pin MRTD and RTMR0-2: $DSTACK_REFERENCE_QUOTE_PATH"
		            exit 1
		        fi
		        TDX_REFERENCE_QUOTE_HEX=$(tr -d '[:space:]' < "$DSTACK_REFERENCE_QUOTE_PATH")
		        TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0x}
		        TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0X}
		        if ! printf '%s' "$TDX_REFERENCE_QUOTE_HEX" | grep -Eq '^[0-9a-fA-F]+$'; then
		            echo "Invalid hex-encoded dstack reference quote: $DSTACK_REFERENCE_QUOTE_PATH"
		            exit 1
		        fi
		        echo "Configuring expected dstack MRTD and RTMR0-2 from reference quote: $DSTACK_REFERENCE_QUOTE_PATH"
		        cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
		            $DCAP_TDX_V4_ADDRESS "setExpectedDstackMeasurementsFromQuote(bytes)" "0x$TDX_REFERENCE_QUOTE_HEX"

		        if [ "${PHALA_ENFORCE_REFERENCE_RTMR3:-0}" = "1" ]; then
		            TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0x}
		            TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0X}
		            echo "Configuring legacy exact RTMR3 policy from reference quote: $DSTACK_REFERENCE_QUOTE_PATH"
		            cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
		                $DCAP_TDX_V4_ADDRESS "setExpectedRtmr3FromQuote(bytes)" "0x$TDX_REFERENCE_QUOTE_HEX"
		        fi
            # Legacy verifier selectors can optionally enforce one reference RTMR3.
            # registerDeviceWithAttestedAppCompose uses the structured event-log
            # verifier and deliberately does not consult this exact-value policy.
            authorize_pccs_reader "$DCAP_TDX_V4_ADDRESS"
            cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
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
            cast rpc --rpc-url $rpc_url evm_mine
            fi
        fi

	        if ! forge script script/UploadPccsCollaterals.s.sol --broadcast --rpc-url $rpc_url --ffi; then
	            echo "PCCS collateral upload failed"
	            exit 1
	        fi
	        log_broadcast_gas_cost "upload_pccs_collaterals" "./broadcast/UploadPccsCollaterals.s.sol/$CHAIN_ID/run-latest.json"
	        echo "PCCS Collaterals uploaded successfully"

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


#export ADDRESS_1=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
#export ADDRESS_2=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
#export ADDRESS_3=0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC
#export ADDRESS_4=0x90F79bf6EB2c4f870365E785982E1f101E93b906

export ADDRESS_0=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
export ADDRESS_1=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
export ADDRESS_2=0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC
export ADDRESS_3=0x90F79bf6EB2c4f870365E785982E1f101E93b906
export ADDRESS_4=0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65
export ADDRESS_5=0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc
export ADDRESS_6=0x976EA74026E726554dB657fA54763abd0C3a0aa9
export ADDRESS_7=0x14dC79964da2C08b23698B3D3cc7Ca32193d9955
export ADDRESS_8=0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f
export ADDRESS_9=0xa0Ee7A142d267C1f36714E4a8F75612F20a79720
export ADDRESS_10=0xBcd4042DE499D14e55001CcbB24a551F3b954096
export ADDRESS_11=0x71bE63f3384f5fb98995898A86B02Fb2426c5788
export ADDRESS_12=0xFABB0ac9d68B0B445fB7357272Ff202C5651694a
export ADDRESS_13=0x1CBd3b2770909D4e10f157cABC84C7264073C9Ec
export ADDRESS_14=0xdF3e18d64BC6A983f673Ab319CCaE4f1a57C7097
export ADDRESS_15=0xcd3B766CCDd6AE721141F452C550Ca635964ce71
export ADDRESS_16=0x2546BcD3c84621e976D8185a91A922aE77ECEc30
export ADDRESS_17=0xbDA5747bFD65F08deb54cb465eB87D40e51B197E
export ADDRESS_18=0xdD2FD4581271e230360230F9337D5c0430Bf44C0
export ADDRESS_19=0x8626f6940E2eb28930eFb4CeF49B2d1F2C9C1199

export PRIVATE_KEY_0="$ETH_WALLET_PRIVATE_KEY"

# openssl pkey -pubin -in public_key.pem -outform DER -out public_key.der
# xxd -p public_key.der | tr -d '\n'


echo "========= node server environment variables ========="

echo "REGISTRY_ADDRESS= $DEVICE_REGISTRY_ADDRESS" 

echo "AGGREGATOR_ADDRESS= $AGGREGATOR_SELECTION_ADDRESS" 

echo "GM_STORAGE_ADDRESS= $GMSTORAGE"

echo "MEDICAL_SIGNER_REGISTRY_ADDRESS= $MEDICAL_SIGNER_REGISTRY_ADDRESS"

#echo "ACCOUNT_ADDRESS= $ADDRESS_1"
echo "ACCOUNT_ADDRESS= $ADDRESS_0"

echo "SIGNING_ACCOUNT= $ADDRESS_0"

echo "RPC_URL= $rpc_url"

echo "AUTOMATA_DCAP_V3_ ATTESTATION_URL= ${DCAP_ADDRESS:-}"

echo "AUTOMATA_DCAP_TDX_V4_ATTESTATION_URL= ${DCAP_TDX_V4_ADDRESS:-}"

echo "===================================================="

#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_1 $AGGREGATOR_SELECTION_ADDRESS "setGMStorageAddress(address)" $GMSTORAGE
cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 \
    $AGGREGATOR_SELECTION_ADDRESS "setGMStorageAddress(address)" $GMSTORAGE

echo "GMStorage Addresse wurde in AggregatorSelection gesetzt"

AGGREGATOR_TIMEOUT_REPORT_PERCENT=${AGGREGATOR_TIMEOUT_REPORT_PERCENT:-50}
cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 \
    $AGGREGATOR_SELECTION_ADDRESS "setTimeoutReportThresholdPercent(uint256)" "$AGGREGATOR_TIMEOUT_REPORT_PERCENT"

echo "Aggregator timeout report threshold wurde auf ${AGGREGATOR_TIMEOUT_REPORT_PERCENT}% gesetzt"

LOCAL_TDX_IMAGE_DIGEST=${LOCAL_TDX_IMAGE_DIGEST:-7849ee527ff2efc746c58f67cd6336572d5c71743c608bbd3810079289c7c066}
LOCAL_TDX_MOCK=${LOCAL_TDX_MOCK:-0}

if [ "$LOCAL_TDX_MOCK" = "1" ]; then
    if [ "${DOCKER:-}" = "phala" ] || ! using_local_runtime_services || [ "$CHAIN_ID" != "31337" ]; then
        echo "Refusing to configure the mock TDX verifier outside local Anvil chain 31337."
        exit 1
    fi
    echo "WARNING: deploying LOCAL-ONLY mock TDX verifier; no hardware attestation is provided."
    MOCK_TDX_CREATE_JSON=$(forge create src/attestation/MockTdxV4Attestation.sol:MockTdxV4Attestation \
        --force --no-cache --broadcast --rpc-url "$rpc_url" --private-key "$ETH_WALLET_PRIVATE_KEY" --json)
    export MOCK_TDX_V4_ADDRESS=$(printf '%s' "$MOCK_TDX_CREATE_JSON" | jq -re '.deployedTo // .deployed_to')
    cast send --rpc-url "$rpc_url" --private-key "$ETH_WALLET_PRIVATE_KEY" \
        "$DEVICE_REGISTRY_ADDRESS" "setTdxV4Attestation(address)" "$MOCK_TDX_V4_ADDRESS" >/dev/null
    EXPECTED_WORKER_IMAGE_DIGEST=$LOCAL_TDX_IMAGE_DIGEST
else
    EXPECTED_WORKER_IMAGE_DIGEST=$(extract_worker_image_digest || true)
fi

EXPECTED_WORKER_IMAGE_DIGEST=${EXPECTED_WORKER_IMAGE_DIGEST#0x}
EXPECTED_WORKER_IMAGE_DIGEST=${EXPECTED_WORKER_IMAGE_DIGEST#0X}
if ! printf '%s' "$EXPECTED_WORKER_IMAGE_DIGEST" | grep -Eq '^[0-9a-fA-F]{64}$'; then
    echo "A digest-pinned EXPECTED_WORKER_IMAGE is required; DeviceRegistry image policy is fail-closed."
    exit 1
fi
echo "Expected worker image digest set to sha256:$EXPECTED_WORKER_IMAGE_DIGEST"
cast send --rpc-url "$rpc_url" --private-key "$ETH_WALLET_PRIVATE_KEY" \
    "$DEVICE_REGISTRY_ADDRESS" "setExpectedWorkerImageDigest(bytes32)" "0x$EXPECTED_WORKER_IMAGE_DIGEST" >/dev/null

#echo "Authorization Status:"
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_1)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_1: yes || echo $ADDRESS_1: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_2)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_2: yes || echo $ADDRESS_2: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_3)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_3: yes || echo $ADDRESS_3: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_4)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_4: yes || echo $ADDRESS_4: no


#echo "Authorized Workers 2 to 4"
#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_1 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_2
#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_1 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_3
#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_1 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_4

#echo "Authorization Status:"
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_1)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_1: yes || echo $ADDRESS_1: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_2)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_2: yes || echo $ADDRESS_2: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_3)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_3: yes || echo $ADDRESS_3: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_4)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_4: yes || echo $ADDRESS_4: no

echo "Authorization Status:"
[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_0)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_0: yes || echo $ADDRESS_0: no
[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_1)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_1: yes || echo $ADDRESS_1: no
[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_2)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_2: yes || echo $ADDRESS_2: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_3)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_3: yes || echo $ADDRESS_3: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_4)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_4: yes || echo $ADDRESS_4: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_5)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_5: yes || echo $ADDRESS_5: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_6)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_6: yes || echo $ADDRESS_6: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_7)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_7: yes || echo $ADDRESS_7: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_8)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_8: yes || echo $ADDRESS_8: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_9)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_9: yes || echo $ADDRESS_9: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_10)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_10: yes || echo $ADDRESS_10: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_11)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_11: yes || echo $ADDRESS_11: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_12)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_12: yes || echo $ADDRESS_12: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_13)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_13: yes || echo $ADDRESS_13: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_14)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_14: yes || echo $ADDRESS_14: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_15)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_15: yes || echo $ADDRESS_15: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_16)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_16: yes || echo $ADDRESS_16: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_17)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_17: yes || echo $ADDRESS_17: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_18)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_18: yes || echo $ADDRESS_18: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_19)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_19: yes || echo $ADDRESS_19: no

#echo "Authorized Workers 0 to 19"
#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_0
#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_1
#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_2
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_3
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_4
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_5
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_6
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_7
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_8
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_9
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_10
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_11
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_12
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_13
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_14
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_15
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_16
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_17
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_18
# cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 $DEVICE_REGISTRY_ADDRESS "authorizeAddress(address)" $ADDRESS_19

echo "Authorization Status:"
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_0)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_0: yes || echo $ADDRESS_0: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_1)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_1: yes || echo $ADDRESS_1: no
#[ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_2)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_2: yes || echo $ADDRESS_2: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_3)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_3: yes || echo $ADDRESS_3: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_4)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_4: yes || echo $ADDRESS_4: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_5)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_5: yes || echo $ADDRESS_5: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_6)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_6: yes || echo $ADDRESS_6: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_7)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_7: yes || echo $ADDRESS_7: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_8)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_8: yes || echo $ADDRESS_8: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_9)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_9: yes || echo $ADDRESS_9: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_10)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_10: yes || echo $ADDRESS_10: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_11)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_11: yes || echo $ADDRESS_11: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_12)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_12: yes || echo $ADDRESS_12: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_13)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_13: yes || echo $ADDRESS_13: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_14)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_14: yes || echo $ADDRESS_14: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_15)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_15: yes || echo $ADDRESS_15: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_16)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_16: yes || echo $ADDRESS_16: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_17)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_17: yes || echo $ADDRESS_17: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_18)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_18: yes || echo $ADDRESS_18: no
# [ "$(cast call --rpc-url $rpc_url $DEVICE_REGISTRY_ADDRESS "isAuthorized(address)" $ADDRESS_19)" = "0x$(printf '%063d1')" ] && echo $ADDRESS_19: yes || echo $ADDRESS_19: no

if [ "$IPFS_PROVIDER" = "kubo" ] && [ -n "${INITIAL_GM_CID:-}" ] && [ -n "${INITIAL_GM_SIG_CID:-}" ]; then
    echo "Copying the initial GM artifacts into IPFS MFS via ${KUBO_API_URL}"
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/rm?arg=/start&force=true" >/dev/null || true
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/rm?arg=/start.sig&force=true" >/dev/null || true
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/cp?arg=/ipfs/${INITIAL_GM_CID}&arg=/start&parents=true"
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/cp?arg=/ipfs/${INITIAL_GM_SIG_CID}&arg=/start.sig&parents=true"
fi

prepare_encrypted_initial_gm
# Publish the runtime contract manifest only once the full bootstrap path
# completed, so workers don't resolve endpoints before the runtime is ready.
publish_runtime_contract_manifest
publish_runtime_ready_marker

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
