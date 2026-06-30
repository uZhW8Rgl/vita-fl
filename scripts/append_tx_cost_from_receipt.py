#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path


CSV_HEADER = [
    "timestamp_unix_ms",
    "scope",
    "operation",
    "phase",
    "transactionHash",
    "blockNumber",
    "from",
    "to",
    "contractAddress",
    "gasUsed",
    "effectiveGasPriceWei",
    "effectiveGasPriceGwei",
    "costEth",
    "costEur",
    "ethEurPrice",
    "account",
    "deviceId",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append one on-chain transaction receipt to data/evaluation/transaction_costs.csv.",
    )
    parser.add_argument("tx_hash")
    parser.add_argument("scope")
    parser.add_argument("operation")
    parser.add_argument("--phase", default="")
    parser.add_argument("--rpc-url", default=os.environ.get("RPC_URL", ""))
    parser.add_argument("--csv-path", default="data/evaluation/transaction_costs.csv")
    parser.add_argument("--eth-eur-price", type=Decimal, default=Decimal(os.environ.get("ETH_EUR_PRICE", "3000")))
    parser.add_argument("--account", default=os.environ.get("ACCOUNT_ADDRESS", ""))
    parser.add_argument("--device-id", default=os.environ.get("DEVICE_ID", ""))
    parser.add_argument("--timestamp-unix-ms", type=int, default=int(time.time() * 1000))
    return parser.parse_args()


def cast_receipt(tx_hash: str, rpc_url: str) -> dict:
    if not rpc_url:
        raise SystemExit("Missing --rpc-url / RPC_URL")
    if shutil.which("cast"):
        cmd = ["cast", "receipt", tx_hash, "--json", "--rpc-url", rpc_url]
    else:
        cmd = [
            "docker", "compose", "run", "--rm", "--no-deps", "smart-contracts",
            "cast", "receipt", tx_hash, "--json", "--rpc-url", rpc_url,
        ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def decimal_str(value: Decimal, places: int | None = None) -> str:
    if places is not None:
        quant = Decimal("1").scaleb(-places)
        value = value.quantize(quant)
    return format(value.normalize(), "f")


def parse_numeric(value) -> Decimal:
    text = str(value or "0").strip()
    if text.startswith(("0x", "0X")):
        return Decimal(int(text, 16))
    return Decimal(text)


def load_existing_hashes(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open(newline="") as handle:
        return {
            str(row.get("transactionHash", "")).lower()
            for row in csv.DictReader(handle)
            if row.get("transactionHash")
        }


def append_row(csv_path: Path, row: dict) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with csv_path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        if write_header:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in CSV_HEADER})


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv_path)
    tx_hash_normalized = args.tx_hash.lower()
    if tx_hash_normalized in load_existing_hashes(csv_path):
        print(f"transaction {args.tx_hash} already present in {csv_path}")
        return 0

    receipt = cast_receipt(args.tx_hash, args.rpc_url)
    gas_used = parse_numeric(receipt.get("gasUsed", 0))
    gas_price_wei = parse_numeric(receipt.get("effectiveGasPrice", 0))
    cost_eth = (gas_used * gas_price_wei) / Decimal("1000000000000000000")
    row = {
        "timestamp_unix_ms": args.timestamp_unix_ms,
        "scope": args.scope,
        "operation": args.operation,
        "phase": args.phase,
        "transactionHash": receipt.get("transactionHash", args.tx_hash),
        "blockNumber": str(int(parse_numeric(receipt.get("blockNumber", 0)))),
        "from": receipt.get("from", ""),
        "to": receipt.get("to", ""),
        "contractAddress": receipt.get("contractAddress", ""),
        "gasUsed": str(int(gas_used)),
        "effectiveGasPriceWei": str(int(gas_price_wei)),
        "effectiveGasPriceGwei": decimal_str(gas_price_wei / Decimal("1000000000"), 9),
        "costEth": decimal_str(cost_eth, 18),
        "costEur": decimal_str(cost_eth * args.eth_eur_price, 18),
        "ethEurPrice": decimal_str(args.eth_eur_price),
        "account": args.account,
        "deviceId": args.device_id,
    }
    append_row(csv_path, row)
    print(json.dumps(row, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
