"""Validate explicit RA-TLS deployment policy without deriving trust or keys."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Mapping

from nacl.signing import SigningKey

VERIFY_URL = "https://cloud-api.phala.com/api/v1/attestations/verify"
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MEASUREMENTS = {"mrtd", "rtmr0", "rtmr1", "rtmr2"}


def read_env_file(path: str | Path) -> dict[str, str]:
    """Read raw KEY=value entries like tf-env.sh: last entry wins, no evaluation.

    Quotes, inline comments and carriage returns remain part of the value.
    Reading bytes avoids Python's universal-newline conversion changing values.
    """
    try:
        content = Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Deployment environment must be UTF-8 text") from exc
    values = {}
    for line in content.split("\n"):
        key, separator, value = line.partition("=")
        if separator and _ENV_KEY.fullmatch(key):
            values[key] = value
    return values


def decode_key(value: str, name: str) -> bytes:
    """Decode a 32-byte hex/base64 key without including its value in errors."""
    message = f"{name} must encode exactly 32 bytes as hex or base64"
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError(message)
    encoded = value.strip().removeprefix("0x")
    try:
        decoded = (
            bytes.fromhex(encoded)
            if re.fullmatch(r"[0-9a-fA-F]{64}", encoded)
            else base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(message) from exc
    if len(decoded) != 32:
        raise ValueError(message)
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field")
        result[key] = value
    return result


def parse_registry(raw: str) -> dict[str, bytes]:
    """Decode explicit subject bindings, rejecting duplicate subjects or keys."""
    message = "AGENT_POP_REGISTRY must be a nonempty JSON object of unique agent subjects and 32-byte public keys"
    try:
        if len(raw) > 256 * 1024:
            raise ValueError(message)
        registry = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(registry, dict) or not 1 <= len(registry) <= 1024:
            raise ValueError(message)
        result = {}
        for subject, key in registry.items():
            if not _SUBJECT.fullmatch(subject):
                raise ValueError(message)
            result[subject] = decode_key(key, "AGENT_POP_REGISTRY")
        if len(set(result.values())) != len(result):
            raise ValueError(message)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(message) from exc


def _platform_policy(raw: str) -> None:
    message = (
        "RATLS_ALLOWED_PLATFORM_MEASUREMENTS must contain 1 to 32 JSON objects with exactly "
        "mrtd, rtmr0, rtmr1 and rtmr2, each a 96-character hexadecimal measurement"
    )
    try:
        if len(raw) > 256 * 1024:
            raise ValueError(message)
        policy = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(policy, list) or not 1 <= len(policy) <= 32:
            raise ValueError(message)
        for entry in policy:
            if not isinstance(entry, dict) or set(entry) != _MEASUREMENTS:
                raise ValueError(message)
            if any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{96}", value) for value in entry.values()
            ):
                raise ValueError(message)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(message) from exc


def validate_ratls_config(values: Mapping[str, str]) -> None:
    """Reject incomplete/inconsistent operator policy with safe aggregated errors.

    Worker 0 always needs a receiver registry. Agent secrets and platform policy
    are required only when the bundled agent is enabled by tf-env.sh's true/1.
    """
    agent_enabled = values.get("ENABLE_PHALA_AGENT", "") in {"true", "1"}
    required = ["AGENT_POP_REGISTRY"]
    if agent_enabled:
        required += ["AGENT_POP_SIGNING_SEED", "RATLS_ALLOWED_PLATFORM_MEASUREMENTS"]
    missing = [name for name in required if not values.get(name)]
    errors = ["Missing required variables: " + ", ".join(missing)] if missing else []

    registry = None
    if values.get("AGENT_POP_REGISTRY"):
        try:
            registry = parse_registry(values["AGENT_POP_REGISTRY"])
        except ValueError as exc:
            errors.append(str(exc))

    hostname = values.get("PKI_WORKER_DNS_NAME", "")
    if hostname and (
        len(hostname) > 253
        or len(hostname) < 2
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in hostname.split(".")
        )
    ):
        errors.append("PKI_WORKER_DNS_NAME must be a DNS hostname without a scheme, path or port")
    endpoint = values.get("PHALA_ATTESTATION_VERIFY_URL", "")
    if endpoint and endpoint != VERIFY_URL:
        errors.append("PHALA_ATTESTATION_VERIFY_URL must use the fixed official Phala verification endpoint")

    if agent_enabled:
        subject = values.get("PKI_AGENT_SUBJECT") or "master-thesis-agent"
        if not _SUBJECT.fullmatch(subject):
            errors.append("PKI_AGENT_SUBJECT must be a valid registered agent identifier")
        seed = None
        if values.get("AGENT_POP_SIGNING_SEED"):
            try:
                seed = decode_key(values["AGENT_POP_SIGNING_SEED"], "AGENT_POP_SIGNING_SEED")
            except ValueError as exc:
                errors.append(str(exc))
        if registry is not None:
            if subject not in registry:
                errors.append("PKI_AGENT_SUBJECT must have an entry in AGENT_POP_REGISTRY")
            elif seed is not None and registry[subject] != bytes(SigningKey(seed).verify_key):
                errors.append("AGENT_POP_REGISTRY must match AGENT_POP_SIGNING_SEED for PKI_AGENT_SUBJECT")
        for name in (
            "SELLO_TOKEN_ISSUER_SIGNING_SEED",
            "SELLO_ZK_SERVICE_SIGNING_SEED",
            "SELLO_OWNER_HPKE_PRIVATE_KEY",
        ):
            if values.get(name):
                try:
                    existing = decode_key(values[name], name)
                    if seed is not None and seed == existing:
                        errors.append(f"AGENT_POP_SIGNING_SEED must be independent of {name}")
                except ValueError as exc:
                    errors.append(str(exc))
        if values.get("RATLS_ALLOWED_PLATFORM_MEASUREMENTS"):
            try:
                _platform_policy(values["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"])
            except ValueError as exc:
                errors.append(str(exc))

    if errors:
        raise ValueError("RA-TLS deployment configuration is invalid: " + "; ".join(errors))
