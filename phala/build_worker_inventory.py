#!/usr/bin/env python3
"""Build the encrypted W0-W19 controller inventory from existing env files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


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


def build_inventory(values: dict[str, str], maximum: int) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for slot in range(maximum):
        prefix = f"W{slot}_"
        names = {
            "account_address": prefix + "ACCOUNT_ADDRESS",
            "private_key": prefix + "PRIVATE_KEY",
            "rsa_private_key": prefix + "RSA_PRIVATE_KEY",
            "rsa_public_key": prefix + "RSA_PUBLIC_KEY",
        }
        missing = [name for name in names.values() if not values.get(name, "").strip()]
        if missing:
            raise ValueError(f"worker {slot} is incomplete; missing {', '.join(missing)}")
        inventory.append(
            {
                "slot": slot,
                **{field: values[name] for field, name in names.items()},
            }
        )
    return inventory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", action="append", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 20:
        raise SystemExit("--max-workers must be between 1 and 20")
    inventory = build_inventory(read_env_files(args.env_file), args.max_workers)
    print(json.dumps(inventory, separators=(",", ":")))


if __name__ == "__main__":
    main()
