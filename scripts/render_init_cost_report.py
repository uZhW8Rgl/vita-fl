#!/usr/bin/env python3
from __future__ import annotations

import csv
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path

ENV_CONTRACT_LABELS = {
    "REGISTRY_ADDRESS": "DeviceRegistry",
    "AGGREGATOR_ADDRESS": "AggregatorSelection",
    "GM_STORAGE_ADDRESS": "GMStorage",
    "DCAP_TDX_V4_ADDRESS": "AutomataDcapTdxV4Attestation",
    "RTMR3_REPLAY_POLICY_ADDRESS": "RTMR3ReplayPolicy",
    "ENCLAVE_IDENTITY_HELPER": "EnclaveIdentityHelper",
    "FMSPC_TCB_HELPER": "FmspcTcbHelper",
    "X509_HELPER": "PCKHelper",
    "X509_CRL_HELPER": "X509CRLHelper",
    "PCCS_STORAGE": "AutomataDaoStorage",
    "PCS_DAO": "AutomataPcsDao",
    "ENCLAVE_ID_DAO": "AutomataEnclaveIdentityDao",
    "FMSPC_TCB_DAO": "AutomataFmspcTcbDao",
}


def load_env_address_labels(env_path: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not env_path.exists():
        return labels
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        label = ENV_CONTRACT_LABELS.get(key.strip())
        if not label:
            continue
        address = value.strip().lower()
        if address.startswith("0x") and len(address) == 42:
            labels[address] = label
    return labels


def load_broadcast_metadata(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    address_labels: dict[str, str] = {}
    tx_labels: dict[str, str] = {}
    for path in root.rglob("run-latest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for tx in data.get("transactions", []):
            contract_name = tx.get("contractName")
            function_name = tx.get("function")
            tx_hash = str(tx.get("hash") or tx.get("transactionHash") or "").lower()
            contract_address = str(tx.get("contractAddress") or "").lower()
            if contract_name and contract_address.startswith("0x") and len(contract_address) == 42:
                address_labels.setdefault(contract_address, contract_name)
            if tx_hash:
                if contract_name:
                    tx_labels.setdefault(tx_hash, contract_name)
                elif function_name:
                    tx_labels.setdefault(tx_hash, function_name)
    return address_labels, tx_labels


def contract_name(row: dict, address_labels: dict[str, str], tx_labels: dict[str, str]) -> str:
    tx_hash = str(row.get("transactionHash") or "").lower()
    if tx_hash and tx_hash in tx_labels:
        return tx_labels[tx_hash]
    address = str(row.get("to") or row.get("contractAddress") or "").lower()
    if address in address_labels:
        return address_labels[address]
    return address or "unknown"


def dec(value: str) -> Decimal:
    return Decimal(value or "0")


def fmt18(value: Decimal) -> str:
    return f"{value:.18f}"


def fmt9(value: Decimal) -> str:
    return f"{value:.9f}"


def main() -> int:
    csv_path = Path("data/evaluation/transaction_costs.csv")
    summary_csv_path = Path("data/evaluation/sepolia_fresh_initialization_costs.csv")
    summary_md_path = Path("data/evaluation/sepolia_fresh_initialization_costs.md")
    env_labels = load_env_address_labels(Path(".env"))
    broadcast_address_labels, broadcast_tx_labels = load_broadcast_metadata(Path("smart_contracts/broadcast"))
    address_labels = {**broadcast_address_labels, **env_labels}

    with csv_path.open(newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("scope") == "smart_contracts_init"]

    rows.sort(key=lambda row: int(row.get("blockNumber") or 0))

    by_contract = defaultdict(lambda: {"gasUsed": Decimal("0"), "costEth": Decimal("0"), "costEur": Decimal("0")})
    total_eth = Decimal("0")
    total_eur = Decimal("0")

    summary_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_csv_path.open("w", newline="") as handle:
        fieldnames = [
            "operation",
            "phase",
            "contract",
            "transactionHash",
            "blockNumber",
            "gasUsed",
            "effectiveGasPriceGwei",
            "costEth",
            "costEur",
            "ethEurPrice",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            contract = contract_name(row, address_labels, broadcast_tx_labels)
            gas_used = dec(row["gasUsed"])
            cost_eth = dec(row["costEth"])
            cost_eur = dec(row["costEur"])
            by_contract[contract]["gasUsed"] += gas_used
            by_contract[contract]["costEth"] += cost_eth
            by_contract[contract]["costEur"] += cost_eur
            total_eth += cost_eth
            total_eur += cost_eur
            writer.writerow({
                "operation": row["operation"],
                "phase": row["phase"],
                "contract": contract,
                "transactionHash": row["transactionHash"],
                "blockNumber": row["blockNumber"],
                "gasUsed": row["gasUsed"],
                "effectiveGasPriceGwei": row["effectiveGasPriceGwei"],
                "costEth": row["costEth"],
                "costEur": row["costEur"],
                "ethEurPrice": row["ethEurPrice"],
            })

    lines = []
    lines.append("# Sepolia Fresh Initialization Costs")
    lines.append("")
    lines.append("Source: `data/evaluation/transaction_costs.csv`")
    lines.append("")
    lines.append(f"Total initialization cost: `{fmt18(total_eth)} ETH` = `{fmt18(total_eur)} EUR`")
    lines.append("")
    lines.append("## Per Transaction")
    lines.append("")
    lines.append("| Operation | Contract | Tx Hash | Block | Gas Used | Gas Price (Gwei) | Cost (ETH) | Cost (EUR) |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        lines.append(
            f"| `{row['operation']}` | `{contract_name(row, address_labels, broadcast_tx_labels)}` | `{row['transactionHash']}` | "
            f"{row['blockNumber']} | {row['gasUsed']} | {fmt9(dec(row['effectiveGasPriceGwei']))} | "
            f"{fmt18(dec(row['costEth']))} | {fmt18(dec(row['costEur']))} |"
        )
    lines.append("")
    lines.append("## Per Contract")
    lines.append("")
    lines.append("| Contract | Total Gas Used | Total Cost (ETH) | Total Cost (EUR) |")
    lines.append("| --- | ---: | ---: | ---: |")
    for contract, values in sorted(by_contract.items()):
        lines.append(
            f"| `{contract}` | {int(values['gasUsed'])} | {fmt18(values['costEth'])} | {fmt18(values['costEur'])} |"
        )
    lines.append("")
    summary_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary_md_path)
    print(summary_csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
