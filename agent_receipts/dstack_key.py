"""Sello runtime-key adapter; deterministic derivation is local-test-only."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Mapping

from .runtime_keys import SELLO_RUNTIME_KEY, read_runtime_signing_seed

SELLO_TEE_KEY_PATH = "vita-fl/sello/tee-inference/v1"
SELLO_TEE_KEY_PURPOSE = "ed25519-cose-sign1"
SELLO_TEE_KEY_DERIVATION_DOMAIN = b"MasterThesis.sello.tee-inference.app-bound.v1"
DEFAULT_DSTACK_SOCKET = "/var/run/dstack.sock"


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
    runtime_key_reader=read_runtime_signing_seed,
) -> bytes:
    env = os.environ if environment is None else environment
    phala = str(env.get("DOCKER", "")).lower() == "phala"
    provider = str(env.get("SELLO_SERVICE_KEY_PROVIDER") or ("ephemeral" if phala else "env")).lower()
    if phala and provider != ("kms" if env.get("PARTICIPANT_KEY_PROVIDER") == "kms" else "ephemeral"):
        raise RuntimeError("Phala Sello receivers require SELLO_SERVICE_KEY_PROVIDER=ephemeral")
    if not phala and provider != "env":
        raise RuntimeError("Local Sello receivers only support the env test provider")
    if provider in ("ephemeral", "kms"):
        return runtime_key_reader(SELLO_RUNTIME_KEY)
    else:
        if str(env.get("LOCAL_TDX_MOCK", "")) != "1":
            raise RuntimeError("An environment-provided Sello key is only allowed with LOCAL_TDX_MOCK=1")
        if local_root is None:
            raise RuntimeError("A local Sello key root was not supplied")
        root = local_root
    return derive_sello_signing_seed(root, env)
