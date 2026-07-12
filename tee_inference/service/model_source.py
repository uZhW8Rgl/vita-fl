"""Load and authenticate the latest encrypted DFL model as participant W0."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
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


def _contracts_manifest(kubo_api: str) -> dict[str, str]:
    url = kubo_api.rstrip("/") + "/api/v0/files/read?" + urllib.parse.urlencode({"arg": "/runtime/contracts.json"})
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read().decode("utf-8"))
    return {str(key): str(item) for key, item in value.items()}


def _assert_w0_authorized(rpc_url: str, registry: str, address: str, private_key_pem: str) -> None:
    registered_der = read_device_public_key_der(rpc_url, registry, address)
    private_key = serialization.load_pem_private_key(private_key_pem.replace("\\n", "\n").encode(), password=None)
    supplied_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if registered_der != supplied_der:
        raise RuntimeError("W0 RSA private key does not match the authorized DeviceRegistry public key")


def provision_latest_model(target: Path) -> tuple[Path, bytes, dict[str, str]]:
    """Fetch, decrypt and verify the latest GMStorage bundle, then build its manifest."""

    rpc_url = os.environ["RPC_URL"]
    kubo_api = os.environ["KUBO_API"]
    address = os.environ["ACCOUNT_ADDRESS"].lower()
    private_key = os.environ["RSA_PRIVATE_KEY"]
    if not re.fullmatch(r"0x[a-f0-9]{40}", address):
        raise RuntimeError("ACCOUNT_ADDRESS must be a lowercase EVM address")
    contracts = _contracts_manifest(kubo_api)
    gm_storage = contracts.get("GM_STORAGE_ADDRESS") or contracts.get("gmStorage")
    registry = contracts.get("REGISTRY_ADDRESS") or contracts.get("deviceRegistry")
    if not gm_storage or not registry:
        raise RuntimeError("runtime contracts manifest lacks GMStorage or DeviceRegistry")
    _assert_w0_authorized(rpc_url, registry, address, private_key)

    bundle = read_current_bundle_from_contract(rpc_url, gm_storage, registry)
    download = fetch_onchain_bundle(bundle, target, kubo_api)
    verification = verify_download_with_registry(bundle, download, rpc_url, registry)
    if not verification.get("ok"):
        raise RuntimeError("current model failed authorized aggregator signature verification")

    model_path = Path(download["model_path"])
    model_hash = hashlib.sha256(model_path.read_bytes()).digest()
    round_number = int(download["decryption"]["round"])
    chain_id = int(rpc_call(rpc_url, "eth_chainId", []), 16)
    manifest = {
        1: 1,
        2: "master-thesis/chestmnist-dfl",
        3: f"round-{round_number}",
        4: {1: "aggregated.bin", 2: "application/vnd.master-thesis.dfl-model", 3: model_hash, 4: model_path.stat().st_size, 5: "float64-le-v1"},
        5: {1: chain_id, 2: bytes.fromhex(gm_storage[2:]), 3: round_number, 4: bundle["model_cid"], 5: model_hash},
        6: {1: "input", 2: "float64", 3: [1, 784], 4: "logits", 5: "float64", 6: [1, 14], 7: "row-major"},
        7: {1: "uint8-grayscale", 2: [28, 28, 1], 3: "row-major-hwc", 4: "(float64(pixel)-127.5)/127.5", 5: "float64-native-pytorch"},
        8: {1: "multilabel", 2: "sigmoid", 3: "greater-than-or-equal", 4: 500_000},
        9: list(LABELS),
    }
    manifest_bytes = encode_deterministic(manifest)
    manifest_path = target / "model-manifest.cbor"
    manifest_path.write_bytes(manifest_bytes)
    stable_model_path = target / "aggregated.bin"
    stable_model_path.write_bytes(model_path.read_bytes())
    return stable_model_path, manifest_bytes, bundle

