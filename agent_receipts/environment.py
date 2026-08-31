"""Environment adapters for the Sello v1 prototype profile."""

from __future__ import annotations

import json
import os
from typing import Mapping

from nacl.signing import SigningKey, VerifyKey

from .dstack_key import tee_sello_signing_seed
from .sello_v1 import ReceiptVerificationError, SelloOwner, SelloReceiver, b64url_decode, b64url_encode

RECEIPT_HEADER = "X-Sello-Receipt"


def _key_bytes(value: str, name: str) -> bytes:
    text = value.strip()
    if text.startswith("0x"):
        text = text[2:]
    try:
        raw = bytes.fromhex(text) if len(text) == 64 else b64url_decode(text)
    except Exception as exc:
        raise ReceiptVerificationError(f"{name} is not a valid 32-byte key") from exc
    if len(raw) != 32:
        raise ReceiptVerificationError(f"{name} must contain exactly 32 bytes")
    return raw


def receiver_from_environment(service_id: str) -> SelloReceiver | None:
    seed = os.environ.get("SELLO_SERVICE_SIGNING_SEED", "")
    issuer = os.environ.get("SELLO_TOKEN_ISSUER_PUBLIC_KEY", "")
    required = os.environ.get("SELLO_REQUIRED", "0").lower() in {"1", "true", "yes"}
    if service_id == "tee-inference":
        if not issuer:
            if required:
                raise RuntimeError("Sello token issuer key is required but not configured")
            return None
        phala = os.environ.get("DOCKER", "").lower() == "phala"
        provider = os.environ.get("SELLO_SERVICE_KEY_PROVIDER", "dstack" if phala else "env").lower()
        if provider == "env" and not seed:
            if required:
                raise RuntimeError("Local Sello receiver key root is required but not configured")
            return None
        local_root = _key_bytes(seed, "SELLO_SERVICE_SIGNING_SEED") if provider == "env" else None
        derived_seed = tee_sello_signing_seed(os.environ, local_root=local_root)
        return SelloReceiver(
            service_id,
            derived_seed,
            _key_bytes(issuer, "SELLO_TOKEN_ISSUER_PUBLIC_KEY"),
        )
    if not seed or not issuer:
        if required:
            raise RuntimeError("Sello receiver keys are required but not configured")
        return None
    return SelloReceiver(service_id, _key_bytes(seed, "SELLO_SERVICE_SIGNING_SEED"), _key_bytes(issuer, "SELLO_TOKEN_ISSUER_PUBLIC_KEY"))


def owner_from_environment() -> SelloOwner | None:
    issuer_seed = os.environ.get("SELLO_TOKEN_ISSUER_SIGNING_SEED", "")
    hpke_seed = os.environ.get("SELLO_OWNER_HPKE_PRIVATE_KEY", "")
    registry_raw = os.environ.get("SELLO_SERVICE_REGISTRY", "")
    required = os.environ.get("SELLO_REQUIRED", "0").lower() in {"1", "true", "yes"}
    if not issuer_seed or not hpke_seed or not registry_raw:
        if required:
            raise RuntimeError("Sello owner keys and service registry are required but not configured")
        return None
    try:
        registry_value = json.loads(registry_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SELLO_SERVICE_REGISTRY is not valid JSON") from exc
    if not isinstance(registry_value, dict):
        raise RuntimeError("SELLO_SERVICE_REGISTRY must be a JSON object")
    registry = {
        str(service): VerifyKey(_key_bytes(str(public_key), f"SELLO_SERVICE_REGISTRY[{service}]"))
        for service, public_key in registry_value.items()
    }
    logs = [url.strip() for url in os.environ.get("SELLO_LOG_URLS", os.environ.get("SCITT_URL", "")).split(",") if url.strip()]
    if not logs:
        raise RuntimeError("SELLO_LOG_URLS or SCITT_URL must configure at least one log")
    return SelloOwner(
        SigningKey(_key_bytes(issuer_seed, "SELLO_TOKEN_ISSUER_SIGNING_SEED")),
        _key_bytes(hpke_seed, "SELLO_OWNER_HPKE_PRIVATE_KEY"),
        registry,
        subject=os.environ.get("SELLO_OWNER_SUBJECT", "master-thesis-agent"),
        log_urls=logs,
    )


def bearer_token(headers: Mapping[str, str]) -> str:
    authorization = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise ReceiptVerificationError("missing Sello bearer authorization token")
    return token.strip()


def receipt_header(receipt: bytes) -> str:
    return b64url_encode(receipt)


def decode_receipt_header(value: str | None) -> bytes:
    if not value:
        raise ReceiptVerificationError(f"receiver response lacks {RECEIPT_HEADER}")
    return b64url_decode(value)
