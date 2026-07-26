#!/usr/bin/env python3
"""Build the encrypted W0-W499 controller inventory from one Phala env file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MAX_WORKERS = 500
DEFAULT_CHUNK_BYTES = 60_000
INVENTORY_CHUNK_PREFIX = "DYNAMIC_WORKER_INVENTORY_"


def read_env_files(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _read_worker_pem(values: dict[str, str], *, slot: int, kind: str) -> str:
    prefix = f"W{slot}_RSA_{kind.upper()}_KEY"
    return values.get(prefix, "").strip().replace("\\n", "\n")


def build_inventory(
    values: dict[str, str],
    maximum: int,
) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for slot in range(maximum):
        prefix = f"W{slot}_"
        names = {
            "account_address": prefix + "ACCOUNT_ADDRESS",
            "private_key": prefix + "PRIVATE_KEY",
            "device_id": prefix + "DEVICE_ID",
        }
        missing = [name for name in names.values() if not values.get(name, "").strip()]
        rsa_private_key = _read_worker_pem(
            values,
            slot=slot,
            kind="private",
        )
        rsa_public_key = _read_worker_pem(
            values,
            slot=slot,
            kind="public",
        )
        if not rsa_private_key:
            missing.append(prefix + "RSA_PRIVATE_KEY")
        if not rsa_public_key:
            missing.append(prefix + "RSA_PUBLIC_KEY")
        if missing:
            raise ValueError(f"worker {slot} is incomplete; missing {', '.join(missing)}")
        try:
            device_id = int(values[names["device_id"]])
        except ValueError as exc:
            raise ValueError(f"worker {slot} has a non-integer device ID") from exc
        if device_id != slot:
            raise ValueError(
                f"worker {slot} has device ID {device_id}; expected {slot}"
            )
        inventory.append(
            {
                "slot": slot,
                "account_address": values[names["account_address"]],
                "private_key": values[names["private_key"]],
                "device_id": device_id,
                "rsa_private_key": rsa_private_key,
                "rsa_public_key": rsa_public_key,
            }
        )
    return inventory


def chunk_inventory(
    inventory: list[dict[str, object]],
    *,
    maximum_bytes: int = DEFAULT_CHUNK_BYTES,
) -> dict[str, str]:
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")

    chunks: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for record in inventory:
        candidate = [*current, record]
        encoded = json.dumps(candidate, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= maximum_bytes:
            current = candidate
            continue
        if not current:
            raise ValueError("one worker inventory record exceeds the chunk-size limit")
        chunks.append(current)
        current = [record]
        if len(json.dumps(current, separators=(",", ":")).encode("utf-8")) > maximum_bytes:
            raise ValueError("one worker inventory record exceeds the chunk-size limit")
    if current:
        chunks.append(current)

    return {
        f"{INVENTORY_CHUNK_PREFIX}{index:03d}": json.dumps(chunk, separators=(",", ":"))
        for index, chunk in enumerate(chunks)
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument(
        "--output-format",
        choices=("inventory", "additional-workers", "terraform-var-file"),
        default="inventory",
    )
    args = parser.parse_args()
    if not 1 <= args.max_workers <= MAX_WORKERS:
        raise SystemExit(f"--max-workers must be between 1 and {MAX_WORKERS}")
    inventory = build_inventory(read_env_files([args.env_file]), args.max_workers)
    if args.output_format == "terraform-var-file":
        payload: object = {
            "dynamic_worker_inventory_chunks": chunk_inventory(
                inventory,
                maximum_bytes=args.chunk_bytes,
            )
        }
    elif args.output_format == "additional-workers":
        payload = {
            f"worker{record['slot']}": {
                "app_name": f"master-thesis-dfl-worker-{record['slot']}",
                "account_address": record["account_address"],
                "private_key": record["private_key"],
                "rsa_private_key": record["rsa_private_key"],
                "rsa_public_key": record["rsa_public_key"],
            }
            for record in inventory[1:]
        }
    else:
        payload = inventory
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
