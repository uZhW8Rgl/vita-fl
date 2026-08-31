"""Application-bound Sello receiver key derivation for the combined Worker 0 TEE."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import socket
from pathlib import Path
from typing import Mapping

SELLO_TEE_KEY_PATH = "vita-fl/sello/tee-inference/v1"
SELLO_TEE_KEY_PURPOSE = "ed25519-cose-sign1"
SELLO_TEE_KEY_DERIVATION_DOMAIN = b"MasterThesis.sello.tee-inference.app-bound.v1"
DEFAULT_DSTACK_SOCKET = "/var/run/dstack.sock"


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=30)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _dstack_get_key(socket_path: str = DEFAULT_DSTACK_SOCKET) -> bytes:
    if not Path(socket_path).is_socket():
        raise RuntimeError(f"dstack socket is unavailable: {socket_path}")
    connection = _UnixConnection(socket_path)
    body = json.dumps(
        {"path": SELLO_TEE_KEY_PATH, "purpose": SELLO_TEE_KEY_PURPOSE},
        separators=(",", ":"),
    ).encode()
    try:
        connection.request("POST", "/GetKey", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise RuntimeError(f"dstack /GetKey returned HTTP {response.status}")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("error"):
        raise RuntimeError(f"dstack /GetKey failed: {value.get('error') if isinstance(value, dict) else value!r}")
    key_text = str(value.get("key", "")).removeprefix("0x")
    try:
        root = bytes.fromhex(key_text)
    except ValueError as exc:
        raise RuntimeError("dstack /GetKey returned a non-hex key") from exc
    if len(root) != 32:
        raise RuntimeError("dstack /GetKey must return exactly 32 bytes for the Sello key")
    return root


def _address_bytes(value: str, name: str) -> bytes:
    normalized = value.strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", normalized):
        raise RuntimeError(f"{name} is required for Sello key derivation")
    return bytes.fromhex(normalized[2:])


def _derivation_context(environment: Mapping[str, str]) -> bytes:
    participant = _address_bytes(str(environment.get("ACCOUNT_ADDRESS", "")), "ACCOUNT_ADDRESS")
    registry = _address_bytes(
        str(environment.get("REGISTRY_ADDRESS") or environment.get("EXPECTED_DEVICE_REGISTRY_ADDRESS") or ""),
        "REGISTRY_ADDRESS",
    )
    try:
        chain_id = int(str(environment.get("EXPECTED_CHAIN_ID", "")), 10)
    except ValueError as exc:
        raise RuntimeError("EXPECTED_CHAIN_ID is required for Sello key derivation") from exc
    if chain_id <= 0 or chain_id >= 2**256:
        raise RuntimeError("EXPECTED_CHAIN_ID is outside the Sello key derivation domain")
    return b"".join(
        (
            SELLO_TEE_KEY_DERIVATION_DOMAIN,
            participant,
            chain_id.to_bytes(32, "big"),
            registry,
            b"tee-inference",
        )
    )


def derive_sello_signing_seed(root: bytes | bytearray, environment: Mapping[str, str]) -> bytes:
    if len(root) != 32:
        raise RuntimeError("Sello key provider must return exactly 32 bytes")
    return hmac.new(bytes(root), _derivation_context(environment), hashlib.sha256).digest()


def tee_sello_signing_seed(
    environment: Mapping[str, str] | None = None,
    *,
    local_root: bytes | None = None,
    dstack_get_key=_dstack_get_key,
) -> bytes:
    env = os.environ if environment is None else environment
    phala = str(env.get("DOCKER", "")).lower() == "phala"
    provider = str(env.get("SELLO_SERVICE_KEY_PROVIDER") or ("dstack" if phala else "env")).lower()
    if phala and provider != "dstack":
        raise RuntimeError("Phala Sello receivers require SELLO_SERVICE_KEY_PROVIDER=dstack")
    if not phala and provider != "env":
        raise RuntimeError("Local Sello receivers only support the env test provider")
    if provider == "dstack":
        root = dstack_get_key()
    else:
        if str(env.get("LOCAL_TDX_MOCK", "")) != "1":
            raise RuntimeError("An environment-provided Sello key is only allowed with LOCAL_TDX_MOCK=1")
        if local_root is None:
            raise RuntimeError("A local Sello key root was not supplied")
        root = local_root
    return derive_sello_signing_seed(root, env)
