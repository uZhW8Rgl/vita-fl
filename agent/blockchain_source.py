#!/usr/bin/env python3
"""Read current model/signature CIDs from GMStorage and fetch them from IPFS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ipfs_rag import DEFAULT_DOWNLOAD_DIR, DEFAULT_IPFS_API_URL, cat_path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:  # pragma: no cover - reported at runtime
    hashes = serialization = padding = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_RPC_URL = os.environ.get("RPC_URL") or os.environ.get("SEPOLIA_RPC_URL") or "http://127.0.0.1:8545"
DEFAULT_GM_STORAGE_ADDRESS = os.environ.get("GM_STORAGE_ADDRESS", "")
DEFAULT_REGISTRY_ADDRESS = os.environ.get("REGISTRY_ADDRESS", "")


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def config_value(name: str, cli_value: str | None, env_file: dict[str, str], default: str = "") -> str:
    if cli_value:
        return cli_value
    if os.environ.get(name):
        return os.environ[name]
    return env_file.get(name, default)


def normalize_host_rpc_url(rpc_url: str) -> str:
    rpc_url = rpc_url.strip()
    if _running_in_docker():
        return _replace_loopback_service(rpc_url, {8545: "anvil"})
    if "anvil:8545" in rpc_url:
        return rpc_url.replace("anvil:8545", "127.0.0.1:8545")
    return rpc_url


def normalize_ipfs_api_url(api_url: str) -> str:
    api_url = api_url.strip()
    if _running_in_docker():
        return _replace_loopback_service(api_url, {5001: "ipfs"})
    if "ipfs:5001" in api_url:
        return api_url.replace("ipfs:5001", "127.0.0.1:5001")
    return api_url


def _running_in_docker() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("DOCKER", "").lower() in {"1", "true", "yes"}


def _replace_loopback_service(url: str, service_ports: dict[int, str]) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname
    if hostname not in {"127.0.0.1", "localhost"} or parts.port is None:
        return url
    service_name = service_ports.get(parts.port)
    if service_name is None:
        return url
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"
    netloc = f"{userinfo}{service_name}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def function_selector(signature: str) -> str:
    try:
        from Crypto.Hash import keccak  # type: ignore

        digest = keccak.new(digest_bits=256)
        digest.update(signature.encode("ascii"))
        return "0x" + digest.hexdigest()[:8]
    except ImportError:
        selectors = {
            "getGlobalModel()": "0x2beb6c93",
            "getGlobalModelSignature()": "0xac77077f",
            "getLastRoundsAggregator()": "0x95f17aed",
            "getDevice(address)": "0x00d55318",
            "devices(address)": "0xe7b4cac6",
        }
        if signature not in selectors:
            raise RuntimeError("Keccak dependency missing and no precomputed selector is available.")
        return selectors[signature]


def rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if "error" in result:
        raise RuntimeError(f"JSON-RPC error from {method}: {result['error']}")
    return result["result"]


def decode_abi_string(hex_data: str) -> str:
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(data) < 64:
        raise RuntimeError(f"Cannot decode ABI string from short result: {hex_data}")
    offset = int.from_bytes(data[:32], "big")
    length = int.from_bytes(data[offset : offset + 32], "big")
    raw = data[offset + 32 : offset + 32 + length]
    return raw.decode("utf-8")


def decode_abi_address(hex_data: str) -> str:
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(data) < 32:
        raise RuntimeError(f"Cannot decode ABI address from short result: {hex_data}")
    return "0x" + data[12:32].hex()


def _read_word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 32], "big")


def _decode_dynamic_bytes(data: bytes, offset_word_index: int) -> bytes:
    offset = _read_word(data, offset_word_index * 32)
    length = _read_word(data, offset)
    return data[offset + 32 : offset + 32 + length]


def decode_device_public_key(hex_data: str) -> bytes:
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(data) < 128:
        raise RuntimeError(f"Cannot decode device tuple from short result: {hex_data}")
    authorized = _read_word(data, 0) != 0
    if not authorized:
        raise RuntimeError("Last aggregator is not authorized in DeviceRegistry.")
    return _decode_dynamic_bytes(data, 3)


def encode_address_arg(address: str) -> str:
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        raise RuntimeError(f"Invalid address argument: {address!r}")
    return address.removeprefix("0x").lower().rjust(64, "0")


def eth_call_string(rpc_url: str, contract_address: str, method_signature: str) -> str:
    result = rpc_call(
        rpc_url,
        "eth_call",
        [{"to": contract_address, "data": function_selector(method_signature)}, "latest"],
    )
    return decode_abi_string(result)


def eth_call_address(rpc_url: str, contract_address: str, method_signature: str) -> str:
    result = rpc_call(
        rpc_url,
        "eth_call",
        [{"to": contract_address, "data": function_selector(method_signature)}, "latest"],
    )
    return decode_abi_address(result)


def read_device_public_key_der(rpc_url: str, registry_address: str, device_address: str) -> bytes:
    result = rpc_call(
        rpc_url,
        "eth_call",
        [
            {
                "to": registry_address,
                "data": function_selector("getDevice(address)") + encode_address_arg(device_address),
            },
            "latest",
        ],
    )
    return decode_device_public_key(result)


def validate_contract_addresses(gm_storage_address: str, registry_address: str | None = None) -> None:
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", gm_storage_address):
        raise RuntimeError(f"Invalid GM_STORAGE_ADDRESS: {gm_storage_address!r}")
    if registry_address is not None and not re.fullmatch(r"0x[a-fA-F0-9]{40}", registry_address):
        raise RuntimeError(f"Invalid REGISTRY_ADDRESS: {registry_address!r}")


def read_current_bundle_from_contract(
    rpc_url: str,
    contract_address: str,
    registry_address: str | None = None,
) -> dict[str, str]:
    validate_contract_addresses(contract_address, registry_address)
    model_cid = eth_call_string(rpc_url, contract_address, "getGlobalModel()")
    signature_cid = eth_call_string(rpc_url, contract_address, "getGlobalModelSignature()")
    last_aggregator = eth_call_address(rpc_url, contract_address, "getLastRoundsAggregator()")
    if not model_cid or not signature_cid:
        raise RuntimeError("GMStorage returned an empty model CID or signature CID.")
    bundle = {
        "model_cid": model_cid,
        "signature_cid": signature_cid,
        "last_aggregator": last_aggregator,
        "rpc_url": rpc_url,
        "gm_storage_address": contract_address,
    }
    if registry_address:
        bundle["registry_address"] = registry_address
    return bundle


def normalize_cid_path(cid_or_uri: str) -> str:
    value = cid_or_uri.strip()
    if value.startswith("ipfs://"):
        value = value.removeprefix("ipfs://")
    if value.startswith("/ipfs/"):
        return value
    return f"/ipfs/{value}"


def _artifact_name(cid: str, suffix: str) -> str:
    short_hash = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:12]
    return f"onchain-{short_hash}{suffix}"


def fetch_onchain_bundle(
    bundle: dict[str, str],
    out_dir: Path,
    ipfs_api_url: str = DEFAULT_IPFS_API_URL,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / _artifact_name(bundle["model_cid"], "-aggregated.bin")
    signature_path = out_dir / _artifact_name(bundle["signature_cid"], "-aggregated.bin.sig")
    model_path.write_bytes(cat_path(ipfs_api_url, normalize_cid_path(bundle["model_cid"])))
    signature_path.write_bytes(cat_path(ipfs_api_url, normalize_cid_path(bundle["signature_cid"])))
    manifest_path = out_dir / "onchain_bundle.json"
    manifest = {
        "source": "GMStorage",
        "bundle": bundle,
        "download": {
            "model_path": str(model_path),
            "signature_path": str(signature_path),
            "model_size": model_path.stat().st_size,
            "signature_size": signature_path.stat().st_size,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["download"]["manifest_path"] = str(manifest_path)
    return manifest["download"]


def verify_model_signature(model_path: Path, signature_path: Path, public_key_der: bytes) -> dict[str, Any]:
    if serialization is None or padding is None or hashes is None:
        raise RuntimeError("cryptography is required for signature verification. Install agent/requirements.txt.")
    public_key = serialization.load_der_public_key(public_key_der)
    model_bytes = model_path.read_bytes()
    signature_bytes = signature_path.read_bytes()
    try:
        public_key.verify(signature_bytes, model_bytes, padding.PKCS1v15(), hashes.SHA256())
        ok = True
        error = None
    except Exception as exc:
        ok = False
        error = str(exc)
    return {
        "ok": ok,
        "algorithm": "RSA-SHA256",
        "padding": "PKCS1v15",
        "model_path": str(model_path),
        "signature_path": str(signature_path),
        "signature_size": len(signature_bytes),
        "public_key_der_size": len(public_key_der),
        "error": error,
    }


def verify_download_with_registry(
    bundle: dict[str, str],
    download: dict[str, Any],
    rpc_url: str,
    registry_address: str,
) -> dict[str, Any]:
    validate_contract_addresses(bundle["gm_storage_address"], registry_address)
    public_key_der = read_device_public_key_der(rpc_url, registry_address, bundle["last_aggregator"])
    verification = verify_model_signature(
        Path(download["model_path"]),
        Path(download["signature_path"]),
        public_key_der,
    )
    verification.update(
        {
            "last_aggregator": bundle["last_aggregator"],
            "registry_address": registry_address,
        }
    )
    return verification


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read current model/signature CIDs from GMStorage and fetch them via IPFS."
    )
    parser.add_argument("--rpc-url", default=None)
    parser.add_argument("--gm-storage-address", default=None)
    parser.add_argument("--registry-address", default=None)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--ipfs-api-url", default=DEFAULT_IPFS_API_URL)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify model signature using last aggregator public key.",
    )
    args = parser.parse_args(argv)

    env_file = load_env_file(args.env_file)
    rpc_url = normalize_host_rpc_url(config_value("RPC_URL", args.rpc_url, env_file, DEFAULT_RPC_URL))
    ipfs_api_url = normalize_ipfs_api_url(args.ipfs_api_url)
    address = config_value("GM_STORAGE_ADDRESS", args.gm_storage_address, env_file, DEFAULT_GM_STORAGE_ADDRESS)
    registry_address = config_value("REGISTRY_ADDRESS", args.registry_address, env_file, DEFAULT_REGISTRY_ADDRESS)
    bundle = read_current_bundle_from_contract(rpc_url, address, registry_address=registry_address or None)
    result: dict[str, Any] = {"bundle": bundle}
    if args.fetch:
        download = fetch_onchain_bundle(bundle, args.out_dir, ipfs_api_url=ipfs_api_url)
        result["download"] = download
        if args.verify:
            result["verification"] = verify_download_with_registry(bundle, download, rpc_url, registry_address)
            if not result["verification"]["ok"]:
                print(json.dumps(result, indent=2))
                return 2
    elif args.verify:
        raise RuntimeError("--verify requires --fetch so the model and signature are available locally.")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
