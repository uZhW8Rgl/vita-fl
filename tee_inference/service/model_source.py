"""Load and authenticate the latest encrypted DFL model as a registered participant."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from agent.blockchain_source import (
    fetch_onchain_bundle,
    read_current_bundle_from_contract,
    read_device_public_key_der,
    rpc_call,
    verify_download_with_registry,
)
from tee_inference.protocol.v1 import LABELS, encode_deterministic

_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _contracts_manifest(kubo_api: str) -> dict[str, str] | None:
    """Read optional MFS discovery metadata without making it a trust root."""

    url = kubo_api.rstrip("/") + "/api/v0/files/read?" + urllib.parse.urlencode({"arg": "/runtime/contracts.json"})
    try:
        request = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError("runtime contracts manifest is not valid JSON") from exc

    if not isinstance(value, dict):
        raise RuntimeError("runtime contracts manifest is not a JSON object")
    manifest = {str(key): str(item) for key, item in value.items()}
    gm_storage = (
        manifest.get("gm_storage_address")
        or manifest.get("GM_STORAGE_ADDRESS")
        or manifest.get("gmStorage")
    )
    registry = (
        manifest.get("registry_address")
        or manifest.get("REGISTRY_ADDRESS")
        or manifest.get("deviceRegistry")
    )
    if not gm_storage or not registry:
        raise RuntimeError(
            "runtime contracts manifest does not contain GMStorage and DeviceRegistry"
        )
    return manifest


def _required_expected_address(name: str) -> str:
    value = os.environ.get(name, "")
    if not _EVM_ADDRESS.fullmatch(value):
        raise RuntimeError(f"{name} must be configured as a 20-byte EVM address")
    return value.lower()


def _required_expected_chain_id() -> int:
    value = os.environ.get("EXPECTED_CHAIN_ID", "")
    if not value:
        raise RuntimeError("EXPECTED_CHAIN_ID must be configured")
    try:
        chain_id = int(value, 0)
    except ValueError as exc:
        raise RuntimeError("EXPECTED_CHAIN_ID must be a positive integer") from exc
    if chain_id <= 0:
        raise RuntimeError("EXPECTED_CHAIN_ID must be a positive integer")
    return chain_id


def _normalized_runtime_rpc_url(value: str, name: str) -> str:
    candidate = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a valid runtime RPC URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError(f"{name} must be a plain HTTP(S) runtime RPC endpoint")
    host = parsed.hostname.lower()
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.lower()}://{authority}"


def _verified_contract_trust_root(
    rpc_url: str,
    contracts: dict[str, str],
) -> tuple[str, str, int]:
    """Match runtime discovery against the compose-measured contract trust root."""

    gm_storage = (
        contracts.get("gm_storage_address") or contracts.get("GM_STORAGE_ADDRESS") or contracts.get("gmStorage")
    )
    registry = contracts.get("registry_address") or contracts.get("REGISTRY_ADDRESS") or contracts.get("deviceRegistry")
    if not gm_storage or not registry:
        raise RuntimeError("runtime contracts manifest lacks GMStorage or DeviceRegistry")
    if not _EVM_ADDRESS.fullmatch(gm_storage):
        raise RuntimeError("runtime contracts manifest contains an invalid GMStorage address")
    if not _EVM_ADDRESS.fullmatch(registry):
        raise RuntimeError("runtime contracts manifest contains an invalid DeviceRegistry address")

    expected_gm_storage = _required_expected_address("EXPECTED_GM_STORAGE_ADDRESS")
    expected_registry = _required_expected_address("EXPECTED_DEVICE_REGISTRY_ADDRESS")
    expected_chain_id = _required_expected_chain_id()
    expected_runtime_rpc_url = _normalized_runtime_rpc_url(
        os.environ.get("EXPECTED_RUNTIME_RPC_URL", ""),
        "EXPECTED_RUNTIME_RPC_URL",
    )
    actual_runtime_rpc_url = _normalized_runtime_rpc_url(rpc_url, "RPC_URL")
    if actual_runtime_rpc_url != expected_runtime_rpc_url:
        raise RuntimeError("RPC_URL does not match the compose-measured runtime endpoint")
    gm_storage = gm_storage.lower()
    registry = registry.lower()
    if gm_storage != expected_gm_storage:
        raise RuntimeError("runtime GMStorage address does not match the compose-measured trust root")
    if registry != expected_registry:
        raise RuntimeError("runtime DeviceRegistry address does not match the compose-measured trust root")

    rpc_chain_id = int(rpc_call(rpc_url, "eth_chainId", []), 16)
    if rpc_chain_id != expected_chain_id:
        raise RuntimeError(
            "RPC chain ID does not match the compose-measured trust root "
            f"(expected {expected_chain_id}, received {rpc_chain_id})"
        )
    return gm_storage, registry, rpc_chain_id


def _private_key_pem_from_environment() -> bytes:
    key_file = (os.environ.get("RSA_PRIVATE_KEY_FILE") or "").strip()
    if key_file:
        path = Path(key_file)
        try:
            value = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Could not read RSA_PRIVATE_KEY_FILE {path}: {exc}") from exc
        if not value.strip():
            raise RuntimeError("RSA_PRIVATE_KEY_FILE is empty")
        return value

    inline = (os.environ.get("RSA_PRIVATE_KEY") or "").replace("\\n", "\n").strip()
    if inline:
        return inline.encode()
    raise RuntimeError("RSA_PRIVATE_KEY_FILE or RSA_PRIVATE_KEY is required")


def _assert_w0_authorized(
    rpc_url: str,
    registry: str,
    address: str,
    private_key_pem: str | bytes,
) -> None:
    registered_der = read_device_public_key_der(rpc_url, registry, address)
    encoded_key = (
        private_key_pem.replace("\\n", "\n").encode()
        if isinstance(private_key_pem, str)
        else private_key_pem
    )
    private_key = serialization.load_pem_private_key(encoded_key, password=None)
    supplied_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if registered_der != supplied_der:
        raise RuntimeError("participant RSA private key does not match the authorized DeviceRegistry public key")


def provision_latest_model(target: Path) -> tuple[Path, bytes, dict[str, str]]:
    """Fetch, decrypt and verify the latest GMStorage bundle, then build its manifest."""

    rpc_url = os.environ["RPC_URL"]
    kubo_api = os.environ["KUBO_API"]
    address = os.environ["ACCOUNT_ADDRESS"].lower()
    if not re.fullmatch(r"0x[a-f0-9]{40}", address):
        raise RuntimeError("ACCOUNT_ADDRESS must be a lowercase EVM address")
    contracts = _contracts_manifest(kubo_api)
    manifest_present = contracts is not None
    if contracts is None:
        contracts = {
            "gm_storage_address": _required_expected_address(
                "EXPECTED_GM_STORAGE_ADDRESS"
            ),
            "registry_address": _required_expected_address(
                "EXPECTED_DEVICE_REGISTRY_ADDRESS"
            ),
        }
    gm_storage, registry, chain_id = _verified_contract_trust_root(rpc_url, contracts)
    if manifest_present:
        manifest_chain_id = str(contracts.get("chain_id", "")).strip()
        if not manifest_chain_id.isdigit() or int(manifest_chain_id) != chain_id:
            raise RuntimeError(
                "runtime contracts manifest chain_id does not match the verified runtime chain"
            )
    _assert_w0_authorized(
        rpc_url,
        registry,
        address,
        _private_key_pem_from_environment(),
    )

    bundle = read_current_bundle_from_contract(rpc_url, gm_storage, registry)
    download = fetch_onchain_bundle(bundle, target, kubo_api)
    verification = verify_download_with_registry(bundle, download, rpc_url, registry)
    if not verification.get("ok"):
        raise RuntimeError("current model failed authorized aggregator signature verification")

    model_path = Path(download["model_path"])
    model_hash = hashlib.sha256(model_path.read_bytes()).digest()
    round_number = bundle["finalized_model_round"]
    encrypted_bundle_round = download["decryption"]["round"]
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number <= 0
        or isinstance(encrypted_bundle_round, bool)
        or not isinstance(encrypted_bundle_round, int)
        or encrypted_bundle_round < 0
    ):
        raise RuntimeError("resolved model or encrypted key bundle contains an invalid round")
    if encrypted_bundle_round != round_number:
        raise RuntimeError("encrypted key bundle round does not match the atomically resolved finalized model round")
    manifest = {
        1: 1,
        2: "master-thesis/chestmnist-dfl",
        3: f"round-{round_number}",
        4: {
            1: "aggregated.bin",
            2: "application/vnd.master-thesis.dfl-model",
            3: model_hash,
            4: model_path.stat().st_size,
            5: "float64-le-v1",
        },
        5: {1: chain_id, 2: bytes.fromhex(gm_storage[2:]), 3: round_number, 4: bundle["model_cid"], 5: model_hash},
        6: {1: "input", 2: "float64", 3: [1, 784], 4: "logits", 5: "float64", 6: [1, 14], 7: "row-major"},
        7: {
            1: "uint8-grayscale",
            2: [28, 28, 1],
            3: "row-major-hwc",
            4: "(float64(pixel)-127.5)/127.5",
            5: "float64-native-pytorch",
        },
        8: {1: "multilabel", 2: "sigmoid", 3: "greater-than-or-equal", 4: 500_000},
        9: list(LABELS),
    }
    manifest_bytes = encode_deterministic(manifest)
    manifest_path = target / "model-manifest.cbor"
    manifest_path.write_bytes(manifest_bytes)
    stable_model_path = target / "aggregated.bin"
    stable_model_path.write_bytes(model_path.read_bytes())
    return stable_model_path, manifest_bytes, bundle
