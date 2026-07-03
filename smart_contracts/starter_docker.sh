#!/bin/bash

set -euo pipefail


export ETH_WALLET_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80

echo "Wallet private key wurde gesetzt." 

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

publish_runtime_contract_manifest() {
    if [ "$IPFS_PROVIDER" != "kubo" ]; then
        return 0
    fi

    require_address DEVICE_REGISTRY_ADDRESS "$DEVICE_REGISTRY_ADDRESS"
    require_address AGGREGATOR_SELECTION_ADDRESS "$AGGREGATOR_SELECTION_ADDRESS"
    require_address GMSTORAGE "$GMSTORAGE"

    local manifest_file
    manifest_file=$(mktemp)
    cat >"$manifest_file" <<EOF
{"registry_address":"$DEVICE_REGISTRY_ADDRESS","aggregator_address":"$AGGREGATOR_SELECTION_ADDRESS","gm_storage_address":"$GMSTORAGE","rpc_url":"$rpc_url","chain_id":"$CHAIN_ID"}
EOF

    echo "Publishing runtime contract manifest to Kubo MFS: /runtime/contracts.json"
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        "${KUBO_API_URL}/api/v0/files/mkdir?arg=/runtime&parents=true" >/dev/null || true
    curl --connect-timeout 5 --max-time 15 -sSf -X POST \
        -F "file=@${manifest_file}" \
        "${KUBO_API_URL}/api/v0/files/write?arg=/runtime/contracts.json&create=true&truncate=true&parents=true" >/dev/null
    rm -f "$manifest_file"
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
        echo "Skipping encrypted local bootstrap generation for IPFS_PROVIDER=pinata"
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
    bundle_path=$(printf '%s' "$bootstrap_json" | jq -re '.bundlePath')
    bundle_signature_path=$(printf '%s' "$bootstrap_json" | jq -re '.bundleSignaturePath')
    key_bundle_path=$(printf '%s' "$bootstrap_json" | jq -re '.keyBundlePath')
    recipient_count=$(printf '%s' "$bootstrap_json" | jq -re '.recipientCount')

    echo "Encrypted bootstrap recipients from on-chain registry: $recipient_count"

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
        $GMSTORAGE "setGlobalModelAndSignatureAndKeyBundle(string,string,string)" \
        "$encrypted_model_cid" "$encrypted_sig_cid" "$encrypted_key_cid"
}

prepare_local_initial_gm
forge script --rpc-url $rpc_url --broadcast script/Deploy.s.sol
log_broadcast_gas_cost "deploy_core_contracts" "./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json"

export DEVICE_REGISTRY_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "DeviceRegistry") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export AGGREGATOR_SELECTION_ADDRESS=$(jq -re '.transactions[] | select(.contractName == "AggregatorSelection") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

export GMSTORAGE=$(jq -re '.transactions[] | select(.contractName == "GMStorage") | .contractAddress' ./broadcast/Deploy.s.sol/$CHAIN_ID/run-latest.json)

ENABLE_DCAP=${ENABLE_DCAP:-1}
if [ "$ENABLE_DCAP" = "1" ]; then
    echo "Deploying Automata DCAP v4 contracts"

    export PRIVATE_KEY=$ETH_WALLET_PRIVATE_KEY
    export DCAP_IMAGE_ID=${DCAP_IMAGE_ID:-0x97f41badbcc8d79521f10cd076fa7a2ed67b84abe07c496da11a2a708c9e5f14}

    if [ "${DOCKER:-}" != "phala" ]; then
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
        install_p256_verifier() {
            local verifier=$1
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
            TDX_REFERENCE_QUOTE_PATH=${TDX_REFERENCE_QUOTE_PATH:-../data/phala_tdx_quote}
            if [ -f "$TDX_REFERENCE_QUOTE_PATH" ]; then
                TDX_REFERENCE_QUOTE_HEX=$(tr -d '[:space:]' < "$TDX_REFERENCE_QUOTE_PATH")
                TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0x}
                TDX_REFERENCE_QUOTE_HEX=${TDX_REFERENCE_QUOTE_HEX#0X}
                echo "Configuring expected RTMR3 policy from reference quote: $TDX_REFERENCE_QUOTE_PATH"
                cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                    $DCAP_TDX_V4_ADDRESS "setExpectedRtmr3FromQuote(bytes)" "0x$TDX_REFERENCE_QUOTE_HEX"
            fi
            PHALA_COMPOSE_PATH=${PHALA_COMPOSE_PATH:-../phala/dstack-compose.template.yml}
            if [ -f "$PHALA_COMPOSE_PATH" ]; then
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
                cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                    $DCAP_TDX_V4_ADDRESS "setExpectedComposeHash(bytes32)" "0x$EXPECTED_COMPOSE_HASH"
            fi
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
                    cast send --rpc-url $rpc_url --private-key $ETH_WALLET_PRIVATE_KEY \
                        $DCAP_TDX_V4_ADDRESS "setExpectedComposeEventDigest(bytes)" "0x$EXPECTED_COMPOSE_EVENT_DIGEST"
                fi
            fi
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
#export PRIVATE_KEY_1=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
#export PRIVATE_KEY_2=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
#export PRIVATE_KEY_3=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a
#export PRIVATE_KEY_4=0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6

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

export PRIVATE_KEY_0=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
export PRIVATE_KEY_1=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
export PRIVATE_KEY_2=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a
export PRIVATE_KEY_3=0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6
export PRIVATE_KEY_4=0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a
export PRIVATE_KEY_5=0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba
export PRIVATE_KEY_6=0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e
export PRIVATE_KEY_7=0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356
export PRIVATE_KEY_8=0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97
export PRIVATE_KEY_9=0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6
export PRIVATE_KEY_10=0xf214f2b2cd398c806f84e317254e0f0b801d0643303237d97a22a48e01628897
export PRIVATE_KEY_11=0x701b615bbdfb9de65240bc28bd21bbc0d996645a3dd57e7b12bc2bdf6f192c82
export PRIVATE_KEY_12=0xa267530f49f8280200edf313ee7af6b827f2a8bce2897751d06a843f644967b1
export PRIVATE_KEY_13=0x47c99abed3324a2707c28affff1267e45918ec8c3f20b8aa892e8b065d2942dd
export PRIVATE_KEY_14=0xc526ee95bf44d8fc405a158bb884d9d1238d99f0612e9f33d006bb0789009aaa
export PRIVATE_KEY_15=0x8166f546bab6da521a8369cab06c5d2b9e46670292d85c875ee9ec20e84ffb61
export PRIVATE_KEY_16=0xea6c44ac03bff858b476bba40716402b03e41b8e97e276d1baec7c37d42484a0
export PRIVATE_KEY_17=0x689af8efa8c651a91ad287602527f3af2fe9f6501a7ac4b061667b5a93e037fd
export PRIVATE_KEY_18=0xde9be858da4a475276426320d5e9262ecfc3ba460bfac56360bfa6c4c28b4ee0
export PRIVATE_KEY_19=0xdf57089febbacf7ba0bc227dafbffa9fc08a93fdc68e1e42411a14efcf23656e

# openssl pkey -pubin -in public_key.pem -outform DER -out public_key.der
# xxd -p public_key.der | tr -d '\n'
rsa_public_key=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100a9d2aabf9debc1e7ac4802c8320e783b0c9f022e913f2178ce9d58b3e06b5f6b0588ef051b8fa8467ff7f0c1507d933cd3e885e95f0066b3ae4c09ea7bc4da441cfcda93189922a3b60b6e0d8ca4e391cb0889232994c32f98823c8219ea73c088221b3d3d4ef5a8cafc235b01402a602a8090e2df2c043a5e4905e494c103364ade31539e1fa1203a2068d1095425fc20c415d2cc655ef0ce6474fa92a2722815bbeed19c155cb60b94e923af0176eb5ac90e08b66600524aeb5308b6b04cf6b8266642a2770faf56314d5fbd12fb8c0ce963d10fddfa9ab16f6d979a6dd6fe4a0d6a6b6ba6f0e28ec378b92a38c325924333d13a92c647280b175aa5e4dbad0203010001
rsa_public_key_1=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100b03b4f662c65a12004f6e4f7acb6d7519e588931a6aa65fad799868d81fd6b3d07d47408619ef7bb822c8e725fe079db85302197fe5ea7bb1f7196edfb0b236c68d56ea6d816125dd20f8986aa247e51fddc87974edf311451b03bf6ab3777bd76d39f475a34a93592af66d5cf346aa0af863e9cc43ea848bb519a4d77cb72b30de02df79f2dd3b4ab7e37d633482939488c5d920294bb664a8a631d036d2bf499179dd92424faa77626e37ac32c09cdbdb77561637d3c9a388046fd0293039ceb55f38c21848c5862941355d7af1c6ad2de1250bb4a509e70f6f2e55e2e066cddc99a35ceb06d13fff3edcdce29a4b92e3a1b76902132cb4a78d9334c34c63f0203010001
rsa_public_key_2=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100f2dc6d6de25160cebb4d8871845340a0617da30cfb8fe4732945260bcc89af3ab4ef867be9877f8d6b12699e426c54e3ffc66e3c39e82ffccc00084d5de2ef75d17bcfb416169fd1356d9d5d4d46e525f15624ade7567488a2a996c2e710a94191dd6624ec9e8ff21b0acd97e45994504e856fd97c0e63860308baeaadfba45cc75f38754ced47bd78349e08747f2de420e6263cca8f880a1df561703f6c932e424b14136be22c68d72108eb57d8491d6cba47f0525c570b13e887a73cf39b6775bb50530f4e8fd908c0cd37cd862a0387605c4a63c6ee1b281e4c652894363a5e3d45d459703d43b49bb6afab88716222f483ea59d2074dd183e69a5c19d0cf0203010001
rsa_public_key_3=0x30820122300d06092a864886f70d01010105000382010f003082010a02820101008f452532509419a052190524b78465860e5fe343218cc152af62033b0c88c87687ce0ef3fae6cfe934010351901dc0325fad8f0a3f3563858c111ec7ec84eec4771efbfa539f367e41fe4e879c5ca90505c2eb8f69c5fd066b9191446ebd7cc4671717b347aa512e496790e6acb650cedfbe31dd5b93a0b9fc846f242b39afbba8d2096a4e35266ec7b335f05bc634d6acede82bf0d5b90ac4e5a5f1485d4c8566e63633b78ed34417059bca507814394b2b57d467a36938e9da8555994e78649570f6494ea6a6449109e635a6ffefb3780665a604a59a756a145c242b79241077649279a041300980426ed8ef81121332d55d47f594abdea0c3711124f025590203010001
rsa_public_key_4=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100a529cdd61a5eadd509882b5ddc0940e7298f13ab65e7f36572095c22a6f46c114f8cc6365f4480dc9eee0d2f8a067bf44f4d190aa470ef66a5b508638297dabacd45719dd1a5b27cda3034d55e3b3a4667eff0301485d9e596f56d74969e2ce40c9c86b3d3db5346afacaef03d9c2e0bdd0d7edbe073d04be8326a3f970069f620bf6740d9e4fcbf039c7b77d36a2a6a58b53a508a01c4c62ddd8d4f19adb4190cf7fa648588eb1a37be2c475437cb86521f9e870694745070e4b49761c664c856054c9a7f3b8ede6092bb1daaaeff649fb5dea3a5d27b2f8c4688321e1f302215d4ebbd622102c2a84cc0e79266a9404628d6922333c75f8993826fa57457290203010001
rsa_public_key_5=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100c3ee75b766fe8f26e7bf1551c7a9c6642a067adb56f122c1f29323ef99a3b359cdd21b7ba57fd92dc6f91cfd209736af34762078922779f220b4c3e860af1ed26010e0b142b243abf1a5b680eaf0625df0dc5f033d542b927e3a2ea7c3a0d6908e63b6f6fcd815107e251109ac8c28d6fd7cde638ea71c659006f02d6864416971bf60c97da1c92432578b909d7d0e23ff9cacb99b9eb6e0f84673024eec14d7d4ffad06e167e157b8af489bf1f91d979946fa53727ab17db9893c775c9c025df1a9e601ea6d1b0bc34b5b47a02efdec67b50b086b1fecb5c6bc24a41a80abdc43f3cd2fc2d5ee273eaec228245322b19ada0e3fa309dc6cc36b8b0b6b7bf70f0203010001
rsa_public_key_6=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100bc5947fa5a0b65f00324b0cf328c1154128e2be570ed87811137d2d18548df7561d078992b123aa63faac477f7ee81027e226db1b93f81e7753048aefe005e41a273b1f64f9db28650bd4dc4f8cabcab86b9c36b681ed861bb3ebab33d1f09baf39d3177c13fbd3a60d002a4e87f5fd3afeb9d9d05c112964dcdd6d771fab397e6d0f139222381e2fd4c221eebf2cb45eee030a03dfad012aac5c5e1a876494322e6eed8fa41970f60750366c717874df1449f20a3b4ed338dab58f3f000c66b1644615b48fe0f71a9c6f8dd96cab77697c053c55ce90a87713b1a1cb572187d6e5b7bd25b8629266786f4567f2061e681cf535ea8a8593942508833b85d0bb30203010001
rsa_public_key_7=0x30820122300d06092a864886f70d01010105000382010f003082010a02820101009c40d05cf5084470163589b690b492e3424436b28e056f72fd6b90d61f0ad7e0f1b968f67798a00eb566a5dbc255ac2fe2d4b713dab1a0223c881ab3cd60dff8ace736ab3759444de51b64b33150df4df55389f9a06d074ac3fc14b95b32341f3498141eb793f9a1ef1f56f7f7c3a76778cc96c4bc2859cde2fbc8304f875bb6c8decdeecb8d583a6c3f7b1120c6ca4ed9873b6bdb11b6588db700e38ae9ff3252feec67b1eaabfa66b3e2fb25ca1bcd82cde3a9746aad8256ca3493cb85072be71b42df4c747ff6e1d395a3c142f085a73e21d8da1d2d44a6bb5b9e79a56ab547c44e5eaa4bfcada8fa56c8e2b16456ec9c8c3eb062703446eef7cb43717e830203010001
rsa_public_key_8=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100da0f14dd2372755cb01f45096a805b0a24abeabddf4e17cadb30d23ffb3871d1934182a49d7c44731ba05de80cbfd59aa07a66045463134d1dd3a8eff31c0376ce3a6097dc414a408f8694a38945a1ab231fb974e4e826e9e78e4a8ff94e89767bb918563610b4e585e5c3543211083e8d605c880c787b7a5b47e95bb04f47a0c7163ccead5251731d8c3c190abf047644dd9db1e607cdb7ef6a2014470fc99a9024a106e3bf5338bfe1cfa977f9475c73b7766e14d2d5d3e47ca4287be921763a8a710cbdff3e617edd62472a39043557b9438f30b87631bbb2467e93740ed61035a4b55694af9fb33613824b1a4ba7ba5dc799cf01cb827893c5ef0ace0c630203010001
rsa_public_key_9=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100a861936f8e037495208cae5b0181463b1544c83937d087e371c57fba071447c0f6ce597fd96deea8b8bbc4ecdcb737985ead5ec5b7c1f0e3df645c92aca0e278b05830146f3e452f914f0713fd06752240caa1b01228c3f37be4711fa2786fd04f9bf5f1f4fdfb6ad4a491101f222c15db59ae3a96d9323e764801aa7d571aea8c4ef197546a1391f67499e1b54fbc7e170047d5636afbf169cdbc381b6816dd655d6df9f5e742f9f92749a2f046756a95717132e4f006c798a5cbf712dcfe49d9d7a26fbdecbe34eb548c1e28bcd43d6c7c03c06b03f2e45f8589a82f24d6c67d48cc08136fdc3dd6aeb52d4d5e2685f93a65c2f54f840a3bcb8140261c661f0203010001
rsa_public_key_10=0x30820122300d06092a864886f70d01010105000382010f003082010a02820101009944fe13ab1e7e08df2ae573cfc9f7e27552063a36a581f0119b16f24d91c9364b3b6ea607e608e7c874548a179256c2dad5bbf5bd636846d5a30ad1576353f330ecb32c5217adc3f5a3ab60b616d6aa4cc553b893d8504920b84328fe4cd98544edd521cf60da604437d7b88c0ac98a7c860477a9dd00cc0fa55b4620362f1c2126519caa28a2e60715eb60032304252fffaa288d196f0f0d1737cd7aceb8f93ded7f9cd868c6603b4e46e65e291b6678a752329cf068e365100c2cd9f983bacb1796e167f3596af6a6bd243b30f999bfd107deaee7a655c01e01d064ac8f34c932d873ec5c9deaede031971ee08e1b92a29712d1c6e4e8b76e43591f86c2b90203010001
rsa_public_key_11=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100a287a74f7a928bd37819a67d4a1f97890f636cdb25f0884d7e02f8e5687a57a20a70745e8a850dd7051e5ce499695e3fedfca3eb735bb7f1eaa82279cfdfdafbc5ba0ca3800904c5e75c67b8beb7360f0ad04ca2a39f07bb1bbfaad64aa63f8de44b017115bc9945795bb4381218a0145788e48822b7158823eb21d558daa6c669aab37103878825e127e17cddeb13e25956824e1e0448140dae5e176e90886f5c45ba4acc4ee86fd8a3460ed0f0b47b9b659893d032df8ab42c04b31d19e065e5c8456cf91ce60c960cc4daa351588434280cd271ea321c443accf5e5c487ef7b62a058ae615457b84123a2dc2cdcd022d6255b0988c86ec5507ee18e6c85690203010001
rsa_public_key_12=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100b7f65bc9a97c7ac099ef20fb5f54b7b7850246c8420a7037d1ce6594754715f6db3ccde2346f3bde28c5d8c955ddd927cedcbcd1b288a8fe34405a5a26a60ba82c27d5e55135b6a026de882614621f42426b531bfadd6ff550f528b5517b2770b38351bf9690d38b89c66fa543ad7a650c8ff6bf8db62f1a5a9ca755ac9299a99fcf772329c90a0e14ce90a891c8981b474f14843e00e342550a1d04140db5b62216b434df032c6af96a82e22af0bed05b874d41892e1d484425bc763229b8d3d4705a87320dab5a72833549977204a8f6fed6d3da7bd9d5d590001b3d0bbf586c84e1c3ef6cf8074aa9092558ada8160cf4e1bde492bff7afd1cb749f0e40490203010001
rsa_public_key_13=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100b8d8261e82e0ae25981b9c013418c6649d7173b81b609cc3ac36f930a808be5c01d5f66d975f44788a0d56016dbb930febe68418b2ee7bb0e3c099ff336919c9ee1562caf41d7a9451ef995640e9be7ff56e910188f233ccfc0cb0bed43d14f4044a58af2670086199a00c7a3216ca686741aa194fbc0477b38604ed013b3b93bade571ce5668f4e5295c1c4342288d137103b6daa0b1182535c1bcc52eb795fee758b8bd385a361fc521f8bac14763e0454277bc6ea7647fa497f6fd5b15d574501485521109080c21f6cc6a20021255e9b804df7ab33344da402b1c90f462c64365dee65de862989045625c97f83d7fa69b283c147d454382bac5f4e0a7fad0203010001
rsa_public_key_14=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100a5f9e18938750c92a46ce197c34655a45b3b2bf0ce52bbfd4f73f9d7412a1fcb97733f39783992d3e2b6fac08c550134c048956c11308705696267cc0559d3a664382220bc8c8702f1b2cc117ed7eaf8281cb65f77834328bcc8b7a7ae88204fe73fe672db01d665e7e5b82bc546660e12ba2e93648d4d5b2e1c94d5b828dda3731fb7f97f828ebb92fb5b825dfc044287974fb224c1fbe7a9229183598632b67f522bba8d637347af3aaaf845f888ef56e75385b0ca8f6f874b904f35a6d4eaa169aa5dc094f971e3a8c5cb2ca64cbfbe76827589376bd7f052f5a69b7e557ee731998d500dc563819c1db663d630b623330c5e18a6141bb75148dd753f74c90203010001
rsa_public_key_15=0x30820122300d06092a864886f70d01010105000382010f003082010a02820101008f15d4fa2318174755dd541e2fd9fd2d860d3358be3521b567cdbc4032bf5d416860d84ca374a0a04010f31e336a17e625d47a644dd4b911f28e75b7b35ada3c1850e1cd02c9c7f391816f3f994b3c8a46d08d01fbbeb5de3eb369bc5519106f61384307926789f8b9ab5ce82867803daba0e96d66e28e983d3a383b0226e01d6baf67e211424988d8fc1b8fa0e0bb6ba691551c90563c171478ba8054f33c04a2b348b7b4946d8cc6361e9acf6b0c2462cfb9c789d81778c0fe19c7a3086d7190de77c34355ce3954ebd6531a7de8ffcc173ec3bffc7446b62a72e6f19da6def89992221a8762750d04a3dfef23c24aebacf1e22a2ce75049fec0a4451defb50203010001
rsa_public_key_16=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100d7e9f1e0edfb4ff65b7df1ad30d2ba74d1641d574dfe690643e25f51171a2f08b12f5681b9fb57c9b7ebc3b5a0ab34da0b7c2627596cbcc53d0e18240091161b6fcca7f469fef02f79a7dc4a86cdf9b96a80ce2bdf9cdda47584426990c7537c6d22af72eedf8652de4a30174e6a4cf34f5900b895b490f334021e5a3cae10590fb1520821116d1e5ce442b645407ddfbfcbd75a1ef063af213d25588a0fb859778b3f152cf6ebe297704995ecd593dd1d47bddd50202fb51dc0fcbd22143589b69b62593d891ba613ed7a604ee3d0d6834d5fe3a705c1eac1cd4df623465c50efb4f1556bc97cec739d6816db1cfe018f2995a1e1bdacd08156c8b1d2e4da550203010001
rsa_public_key_17=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100a4d845f26194b3f1b44e271bf85551708777bff554015f528246cb25a846de9c2882dad7f54558a867b7e1040a3de89603f598a965ebaeebac83b2743229dd53881da4a024fffb11e7c4111a39c238093f7b60a43d7e8c217d2d1c04cf779518db92d7e122adc992abec884e198dbef1f8b1b66cd30435dc06e0758da586fcc97c3a7558240af226c620602810b03f76c4ff4956ca9e8af0098090ad27ac687f90e774323dc2bdc9d79abb8e720523a993b9dd41e3504553f7e897c9ecadfe98918fc98a683b5abd78091ee20f3605bedd516ece5a2eb2dc547c4f2d77ba63cca11d30c32f7126c06dc218bdb873110bc09d20a624d0d4baeb6defe2fb169a5f0203010001
rsa_public_key_18=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100bff06babadf46f4c8da3a65ec506eb6b981ea9f1a9d9e4146f5117d43bf23825bb2595e555199eb0aa33ac2a74123054b595857181f88ad3000b6ae3e08311a87550fb00fec5af46ff59070d4ce4cccf2930e8c2c3f94df31c21dee2145f6f7917baf7d61e8e302127a2fdfdd033575a307c70f310a61fdbd09209fd45d89e3770485254a60bf81a327a92003bd152da92c98adb978d968698b5b65e949ee7ee9eebf7082b16ba84b634304edbc624dab5b023bd789fa7e62e845b110af7c1d7cf958592868aae721b048a61a94c82e078e5eedee18db223dded2e3d3ff301d95522021c6a5b435285e6715c8690cc504393acbe6f5d6be6991fce87e7133e790203010001
rsa_public_key_19=0x30820122300d06092a864886f70d01010105000382010f003082010a0282010100beb6a520622f7886c869f96100f604d53e0dea18bedf1079af671a95e0a7137b61cf43dc97f4456ce4e53b06b3582051ceca9518149d1309dd1984d979cd8f0ac3053b96330b638a4ac89f3ccdc8e16da20f96a8c4732944a11420b1514466295f600f5bf0582fbb01c45299490268febe368789931e3393fdedd5798e2252ccf03d2d8d4faa701b3d52f1943a5833774e84e96a5f4a0c4220f3e71a9f068ca1490e53a9806a78f6e0e1ca84ccd2b97273d14bc7e01b8b4989accc0c4cf74236dd446f37d5905b9d6b5ad19cae6132824f69dc8273e7fe07b2fbeb882f597e69f3a1424f970af11414d32f02ed8aa66e21bb176e75dacc9b5111553acba87fb30203010001


echo "========= node server environment variables ========="

echo "REGISTRY_ADDRESS= $DEVICE_REGISTRY_ADDRESS" 

echo "AGGREGATOR_ADDRESS= $AGGREGATOR_SELECTION_ADDRESS" 

echo "GM_STORAGE_ADDRESS= $GMSTORAGE"

#echo "ACCOUNT_ADDRESS= $ADDRESS_1"
echo "ACCOUNT_ADDRESS= $ADDRESS_0"

#echo "PRIVATE_KEY= $PRIVATE_KEY_1"
echo "PRIVATE_KEY= $PRIVATE_KEY_0"

echo "SEPOLIA_RPC_URL= $rpc_url"

echo "AUTOMATA_DCAP_V3_ ATTESTATION_URL= ${DCAP_ADDRESS:-}"

echo "AUTOMATA_DCAP_TDX_V4_ATTESTATION_URL= ${DCAP_TDX_V4_ADDRESS:-}"

echo "===================================================="

#cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_1 $AGGREGATOR_SELECTION_ADDRESS "setGMStorageAddress(address)" $GMSTORAGE
cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 \
    $AGGREGATOR_SELECTION_ADDRESS "setGMStorageAddress(address)" $GMSTORAGE

publish_runtime_contract_manifest


echo "GMStorage Addresse wurde in AggregatorSelection gesetzt"

AGGREGATOR_TIMEOUT_REPORT_PERCENT=${AGGREGATOR_TIMEOUT_REPORT_PERCENT:-50}
cast send --rpc-url $rpc_url --private-key $PRIVATE_KEY_0 \
    $AGGREGATOR_SELECTION_ADDRESS "setTimeoutReportThresholdPercent(uint256)" "$AGGREGATOR_TIMEOUT_REPORT_PERCENT"

echo "Aggregator timeout report threshold wurde auf ${AGGREGATOR_TIMEOUT_REPORT_PERCENT}% gesetzt"


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
