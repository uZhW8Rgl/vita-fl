#!/usr/bin/env python3
"""Migrate one private deployment profile without rotating existing Sello keys."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

from nacl.signing import SigningKey

if __package__:
    from .ratls_config import decode_key, read_env_file, validate_ratls_config
else:
    from ratls_config import decode_key, read_env_file, validate_ratls_config


def encode_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("AGENT_POP_REGISTRY contains duplicate subjects")
        result[key] = value
    return result


def migrate_env_file(
    path: Path,
    *,
    worker_dns_name: str | None = None,
    platform_measurements_file: Path | None = None,
) -> list[str]:
    """Validate a complete candidate before atomically replacing a private file."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("The deployment profile must be an existing regular file")
    original = path.read_bytes()
    values = read_env_file(path)
    updates = {}
    if worker_dns_name is not None:
        updates["PKI_WORKER_DNS_NAME"] = worker_dns_name
    if platform_measurements_file is not None:
        updates["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"] = platform_measurements_file.read_text().strip()

    if values.get("ENABLE_PHALA_AGENT", "") in {"true", "1"}:
        subject = values.get("PKI_AGENT_SUBJECT") or "master-thesis-agent"
        try:
            registry = json.loads(values.get("AGENT_POP_REGISTRY") or "{}", object_pairs_hook=unique_object)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("AGENT_POP_REGISTRY must be valid JSON with unique subjects") from None
        if not isinstance(registry, dict):
            raise ValueError("AGENT_POP_REGISTRY must be a JSON object")
        seed = values.get("AGENT_POP_SIGNING_SEED")
        if not seed:
            if subject in registry:
                raise ValueError(
                    "AGENT_POP_SIGNING_SEED is missing for the registered PKI_AGENT_SUBJECT; "
                    "restore the existing seed instead of replacing its identity"
                )
            seed = encode_key(bytes(SigningKey.generate()))
            updates["AGENT_POP_SIGNING_SEED"] = seed
        public_key = bytes(SigningKey(decode_key(seed, "AGENT_POP_SIGNING_SEED")).verify_key)
        if subject in registry:
            if decode_key(registry[subject], "AGENT_POP_REGISTRY") != public_key:
                raise ValueError("AGENT_POP_REGISTRY does not match AGENT_POP_SIGNING_SEED; refusing to replace it")
        else:
            registry[subject] = encode_key(public_key)
            updates["AGENT_POP_REGISTRY"] = json.dumps(registry, separators=(",", ":"))
        if not values.get("PKI_AGENT_SUBJECT"):
            updates["PKI_AGENT_SUBJECT"] = subject

    candidate = {**values, **updates}
    validate_ratls_config(candidate)
    # Only compact an explicitly supplied policy after the validator has checked
    # the raw JSON, including duplicate fields. Never derive policy from a quote.
    if "RATLS_ALLOWED_PLATFORM_MEASUREMENTS" in updates:
        updates["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"] = json.dumps(
            json.loads(updates["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"]), separators=(",", ":")
        )
    updates = {key: value for key, value in updates.items() if values.get(key) != value}
    if not updates:
        return []
    lines = original.decode("utf-8").splitlines(keepends=True)
    content = "".join(line for line in lines if line.partition("=")[0] not in updates)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f"{key}={value}\n" for key, value in updates.items())
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ratls-env-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if path.read_bytes() != original:
            raise ValueError("The deployment profile changed during migration; retry with the current file")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return sorted(updates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True, help="Existing private deployment profile")
    parser.add_argument(
        "--worker-dns-name", help="Optional explicit Worker 0 TLS-passthrough hostname, without https://"
    )
    parser.add_argument("--platform-measurements-file", type=Path, help="Independently approved OS measurement JSON")
    parser.add_argument("--check", action="store_true", help="Validate the selected file without modifying it")
    args = parser.parse_args()
    if args.check and (args.worker_dns_name is not None or args.platform_measurements_file is not None):
        parser.error("--check validates existing values; omit migration options")
    try:
        if args.check:
            validate_ratls_config(read_env_file(args.env_file))
            print("RA-TLS deployment configuration is valid.")
        else:
            changed = migrate_env_file(
                args.env_file,
                worker_dns_name=args.worker_dns_name,
                platform_measurements_file=args.platform_measurements_file,
            )
            print("Updated variables: " + ", ".join(changed) if changed else "RA-TLS configuration already valid.")
            print("Update PHALA_ENV_CONTENT or re-encrypt the selected PHALA_ENV_ENCRYPTED_FILE before deploying.")
    except (OSError, ValueError) as error:
        print(f"RA-TLS migration: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
