#!/usr/bin/env python3
"""Read current model/signature CIDs from GMStorage and fetch them from IPFS."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from .ipfs_bundle import DEFAULT_DOWNLOAD_DIR, DEFAULT_IPFS_API_URL, cat_path
except ImportError:
    from ipfs_bundle import DEFAULT_DOWNLOAD_DIR, DEFAULT_IPFS_API_URL, cat_path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - reported at runtime
    hashes = serialization = padding = AESGCM = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_RPC_URL = os.environ.get("RPC_URL") or "http://127.0.0.1:8545"
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
            "getGlobalModelKeyBundle()": "0x6f86433c",
            "getLastRoundsAggregator()": "0x95f17aed",
            "getFinalizedModelBundle()": "0xfd419631",
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
    if offset < 0 or offset + 32 > len(data):
        raise RuntimeError("ABI result does not contain the requested 32-byte word.")
    return int.from_bytes(data[offset : offset + 32], "big")


def _decode_dynamic_bytes(data: bytes, offset_word_index: int, *, minimum_offset: int = 0) -> bytes:
    offset = _read_word(data, offset_word_index * 32)
    if offset % 32 != 0:
        raise RuntimeError("ABI dynamic-value offset is not word aligned.")
    if offset < minimum_offset:
        raise RuntimeError("ABI dynamic-value offset overlaps the tuple head.")
    length = _read_word(data, offset)
    start = offset + 32
    end = start + length
    if end > len(data):
        raise RuntimeError("ABI dynamic value exceeds the returned data.")
    return data[start:end]


def decode_finalized_model_bundle(hex_data: str) -> tuple[str, str, str, str, int, bytes]:
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    head_size = 6 * 32
    if len(data) < head_size:
        raise RuntimeError(f"Cannot decode finalized model tuple from short result: {hex_data}")
    try:
        model_cid = _decode_dynamic_bytes(data, 0, minimum_offset=head_size).decode("utf-8")
        signature_cid = _decode_dynamic_bytes(data, 1, minimum_offset=head_size).decode("utf-8")
        key_bundle_cid = _decode_dynamic_bytes(data, 2, minimum_offset=head_size).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Finalized model tuple contains a non-UTF-8 CID.") from exc
    aggregator_word = data[3 * 32 : 4 * 32]
    if aggregator_word[:12] != b"\x00" * 12:
        raise RuntimeError("Finalized model tuple contains non-canonical address padding.")
    last_aggregator = "0x" + aggregator_word[12:32].hex()
    model_round = _read_word(data, 4 * 32)
    publisher_public_key = _decode_dynamic_bytes(data, 5, minimum_offset=head_size)
    if not model_cid or not signature_cid or not key_bundle_cid:
        raise RuntimeError("GMStorage returned an incomplete finalized model bundle.")
    if last_aggregator == "0x" + "00" * 20:
        raise RuntimeError("GMStorage returned an empty finalized model aggregator.")
    if not publisher_public_key:
        raise RuntimeError("GMStorage returned an empty finalized publisher public key.")
    return (
        model_cid,
        signature_cid,
        key_bundle_cid,
        last_aggregator,
        model_round,
        publisher_public_key,
    )


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
) -> dict[str, Any]:
    validate_contract_addresses(contract_address, registry_address)
    result = rpc_call(
        rpc_url,
        "eth_call",
        [
            {
                "to": contract_address,
                "data": function_selector("getFinalizedModelBundle()"),
            },
            "latest",
        ],
    )
    (
        model_cid,
        signature_cid,
        key_bundle_cid,
        last_aggregator,
        model_round,
        publisher_public_key,
    ) = decode_finalized_model_bundle(result)
    bundle = {
        "model_cid": model_cid,
        "signature_cid": signature_cid,
        "key_bundle_cid": key_bundle_cid,
        "last_aggregator": last_aggregator,
        "finalized_model_round": model_round,
        "publisher_public_key_der_hex": publisher_public_key.hex(),
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


def _load_private_key_from_env() -> tuple[Any, str]:
    if serialization is None:
        raise RuntimeError("cryptography is required for encrypted bundle decryption.")
    key_file = (os.environ.get("RSA_PRIVATE_KEY_FILE") or "").strip()
    if key_file:
        return serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None), key_file
    raw_key = (os.environ.get("RSA_PRIVATE_KEY") or "").replace("\\n", "\n").strip()
    if raw_key:
        return serialization.load_pem_private_key(raw_key.encode("utf-8"), password=None), "env:RSA_PRIVATE_KEY"
    raise RuntimeError(
        "Encrypted on-chain model bundle requires RSA_PRIVATE_KEY or RSA_PRIVATE_KEY_FILE for decryption."
    )


def _decrypt_encrypted_bundle(
    bundle: dict[str, str],
    encrypted_model_path: Path,
    key_bundle_path: Path,
    plain_model_path: Path,
    plain_signature_path: Path,
) -> dict[str, Any]:
    if AESGCM is None or padding is None:
        raise RuntimeError("cryptography is required for encrypted bundle decryption.")
    own_address = (os.environ.get("ACCOUNT_ADDRESS") or "").strip().lower()
    if not re.fullmatch(r"0x[a-f0-9]{40}", own_address):
        raise RuntimeError("Encrypted on-chain model bundle requires a valid ACCOUNT_ADDRESS.")

    bundle_doc = json.loads(encrypted_model_path.read_text(encoding="utf-8"))
    key_bundle_doc = json.loads(key_bundle_path.read_text(encoding="utf-8"))
    key_bundle_round = key_bundle_doc.get("round")
    if isinstance(key_bundle_round, bool) or not isinstance(key_bundle_round, int) or key_bundle_round < 0:
        raise RuntimeError("Encrypted global model key bundle contains an invalid round.")
    wrapped_keys = key_bundle_doc.get("wrapped_keys_b64", {})
    wrapped_key_b64 = wrapped_keys.get(own_address)
    if not isinstance(wrapped_key_b64, str) or not wrapped_key_b64:
        raise RuntimeError(f"No wrapped round key found for participant {own_address}.")

    private_key, private_key_source = _load_private_key_from_env()
    key_iv = private_key.decrypt(
        base64.b64decode(wrapped_key_b64),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    if len(key_iv) != 44:
        raise RuntimeError(f"Invalid wrapped round key length {len(key_iv)}; expected 44 bytes.")

    aes_key = key_iv[:32]
    iv = key_iv[32:]
    aesgcm = AESGCM(aes_key)
    ciphertext = base64.b64decode(bundle_doc["ciphertext_b64"])
    auth_tag = base64.b64decode(bundle_doc["auth_tag_b64"])
    plaintext = aesgcm.decrypt(iv, ciphertext + auth_tag, None)
    payload = json.loads(plaintext.decode("utf-8"))
    plain_model_path.write_bytes(base64.b64decode(payload["model_b64"]))
    plain_signature_path.write_bytes(base64.b64decode(payload["signature_b64"]))
    return {
        "encrypted": True,
        "round": key_bundle_round,
        "recipient_address": own_address,
        "private_key_source": private_key_source,
        "plain_model_path": str(plain_model_path),
        "plain_signature_path": str(plain_signature_path),
    }


def fetch_onchain_bundle(
    bundle: dict[str, str],
    out_dir: Path,
    ipfs_api_url: str = DEFAULT_IPFS_API_URL,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    key_bundle_cid = str(bundle.get("key_bundle_cid") or "")
    if not bundle.get("signature_cid"):
        raise RuntimeError("Missing encrypted global model bundle signature CID on-chain.")
    if not key_bundle_cid:
        raise RuntimeError("Missing encrypted global model key bundle CID on-chain.")
    encrypted_bundle = True
    model_suffix = "-aggregated.bundle.enc"
    signature_suffix = "-aggregated.bundle.enc.sig"
    model_path = out_dir / _artifact_name(bundle["model_cid"], model_suffix)
    signature_path = out_dir / _artifact_name(bundle["signature_cid"], signature_suffix)
    model_path.write_bytes(cat_path(ipfs_api_url, normalize_cid_path(bundle["model_cid"])))
    signature_path.write_bytes(cat_path(ipfs_api_url, normalize_cid_path(bundle["signature_cid"])))
    key_bundle_path = None
    plain_model_path = model_path
    plain_signature_path = signature_path
    decrypt_metadata = None
    key_bundle_path = out_dir / _artifact_name(key_bundle_cid, "-aggregated.bundle.keys.json")
    key_bundle_path.write_bytes(cat_path(ipfs_api_url, normalize_cid_path(key_bundle_cid)))
    plain_model_path = out_dir / _artifact_name(bundle["model_cid"], "-aggregated.bin")
    plain_signature_path = out_dir / _artifact_name(bundle["model_cid"], "-aggregated.bin.sig")
    decrypt_metadata = _decrypt_encrypted_bundle(
        bundle,
        model_path,
        key_bundle_path,
        plain_model_path,
        plain_signature_path,
    )
    manifest_path = out_dir / "onchain_bundle.json"
    manifest = {
        "source": "GMStorage",
        "bundle": bundle,
        "download": {
            "model_path": str(plain_model_path),
            "signature_path": str(plain_signature_path),
            "model_size": plain_model_path.stat().st_size,
            "signature_size": plain_signature_path.stat().st_size,
            "encrypted_bundle": encrypted_bundle,
            "encrypted_model_path": str(model_path),
            "encrypted_model_size": model_path.stat().st_size,
            "encrypted_signature_path": str(signature_path) if bundle.get("signature_cid") else "",
            "encrypted_signature_size": signature_path.stat().st_size if signature_path.exists() else 0,
            "key_bundle_path": str(key_bundle_path) if key_bundle_path else "",
            "key_bundle_size": key_bundle_path.stat().st_size if key_bundle_path else 0,
            "decryption": decrypt_metadata,
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
    public_key_hex = str(bundle.get("publisher_public_key_der_hex") or "")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", public_key_hex):
        raise RuntimeError("Finalized model bundle does not contain a valid publisher public key snapshot.")
    public_key_der = bytes.fromhex(public_key_hex)
    encrypted_bundle = bool(download.get("encrypted_bundle"))
    if encrypted_bundle:
        outer_verification = verify_model_signature(
            Path(download["encrypted_model_path"]),
            Path(download["encrypted_signature_path"]),
            public_key_der,
        )
        inner_verification = verify_model_signature(
            Path(download["model_path"]),
            Path(download["signature_path"]),
            public_key_der,
        )
        verification = {
            "ok": outer_verification["ok"] and inner_verification["ok"],
            "outer_bundle_signature": outer_verification,
            "inner_model_signature": inner_verification,
        }
    else:
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
