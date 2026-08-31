#!/usr/bin/env python3
"""Build compact, document-ready assets for the authoritative Phala run.

The script only derives data from the archived export, worker logs, runtime
snapshot, and local ChestMNIST test split. It never contacts a live service.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "data" / "evaluation" / "authoritative-phala-6w-24r-20260830"
RAW_DIR = RUN_DIR / "raw"
TEST_SPLIT = ROOT / "data" / "chestmnist" / "test_data" / "test-data.npz"


def _write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _parse_worker_logs() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    complete_re = re.compile(
        r"Hybrid-R aggregation complete: output_kind=(\w+), selected_candidate=(\w+)"
    )
    field_patterns = {
        "selected_candidate": re.compile(r"selectedCandidate: '([^']+)'"),
        "output_kind": re.compile(r"outputKind: '([^']+)'"),
        "gate_passed": re.compile(r"gatePassed: (true|false)"),
        "parent_validation_loss": re.compile(r"parentLoss: ([0-9.eE+-]+)"),
        "selected_validation_loss": re.compile(r"selectedLoss: ([0-9.eE+-]+)"),
        "validation_data_hash": re.compile(r"validationDataHash: '([^']+)'"),
    }

    for log_path in sorted((RAW_DIR / "workers").glob("vita-fl-worker?-full.log.gz")):
        worker_match = re.search(r"worker(\d+)", log_path.name)
        worker = f"VM-{worker_match.group(1)}" if worker_match else "unknown"
        pending: dict[str, object] = {}
        evidence_target: dict[str, object] | None = None
        in_evidence = False
        with gzip.open(log_path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                complete = complete_re.search(line)
                if complete:
                    pending = {
                        "output_kind": complete.group(1),
                        "selected_candidate": complete.group(2),
                    }
                    continue

                json_start = line.find('{"accuracy_percent"')
                if json_start >= 0:
                    payload = json.loads(line[json_start:])
                    payload.update(pending)
                    payload["aggregator_worker"] = worker
                    payload["experiment_id"] = "phala"
                    payload["federated_round"] = int(payload["source_round"])
                    payload["global_model_round"] = int(payload["round"])
                    records.append(payload)
                    evidence_target = payload
                    pending = {}
                    continue

                if "Hybrid-R selection evidence:" in line:
                    in_evidence = True
                    continue
                if in_evidence and evidence_target is not None:
                    if re.search(r"\}\s*$", line):
                        in_evidence = False
                        continue
                    for field, pattern in field_patterns.items():
                        match = pattern.search(line)
                        if not match:
                            continue
                        value: object = match.group(1)
                        if field == "gate_passed":
                            value = value == "true"
                        elif field.endswith("_loss"):
                            value = float(value)
                        evidence_target[field] = value

    records.sort(key=lambda row: int(row["federated_round"]))
    if len(records) != 24:
        raise RuntimeError(f"Expected 24 evaluation records, found {len(records)}")
    if len({int(row["federated_round"]) for row in records}) != 24:
        raise RuntimeError("Duplicate or missing federated-round records")
    return records


def _worker_gas_records() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Recover confirmed worker transactions from the immutable worker logs.

    The final Prometheus scrape is a useful dashboard snapshot, but it is not
    the accounting source of record: a start-up race in the evaluated control
    service cleared telemetry after workers had begun registering.  The worker
    logs contain the receipt-derived ``gas_cost`` record for every confirmed
    transaction and therefore permit transaction-hash de-duplication without
    estimating or inventing a value.

    Every worker-scoped receipt is included, including the one action-key
    funding transfer per worker, so no executed gas value is silently omitted.
    """

    transactions: list[dict[str, object]] = []
    registrations: list[dict[str, object]] = []
    seen_hashes: set[str] = set()

    for log_path in sorted((RAW_DIR / "workers").glob("vita-fl-worker?-full.log.gz")):
        worker_match = re.search(r"worker(\d+)", log_path.name)
        if worker_match is None:
            raise RuntimeError(f"Could not derive worker identity from {log_path.name}")
        worker = f"VM-{worker_match.group(1)}"
        worker_transactions: list[dict[str, object]] = []
        registration_success_line: int | None = None

        with gzip.open(log_path, "rt", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                if "Device registered with onchain TDX quote and RTMR3 event replay verification" in line:
                    registration_success_line = line_number

                json_start = line.find('{"timestamp_unix_ms"')
                if json_start < 0:
                    continue
                try:
                    payload = json.loads(line[json_start:])
                except json.JSONDecodeError:
                    continue
                if payload.get("kind") != "gas_cost":
                    continue
                if payload.get("scope") != "worker":
                    continue
                transaction_hash = str(payload.get("transactionHash", "")).lower()
                if not re.fullmatch(r"0x[0-9a-f]{64}", transaction_hash):
                    raise RuntimeError(
                        f"Invalid worker transaction hash in {log_path.name}:{line_number}"
                    )
                if transaction_hash in seen_hashes:
                    raise RuntimeError(f"Duplicate worker transaction {transaction_hash}")
                gas_used = int(payload.get("gasUsed", 0))
                if gas_used <= 0:
                    raise RuntimeError(
                        f"Invalid gasUsed in {log_path.name}:{line_number}"
                    )
                record = {
                    "worker": worker,
                    "operation": str(payload.get("operation", "")),
                    "transaction_hash": transaction_hash,
                    "block_number": int(payload.get("blockNumber", 0)),
                    "gas_used": gas_used,
                    "account": str(payload.get("account", "")).lower(),
                    "device_id": str(payload.get("deviceId", "")),
                    "log_file": log_path.name,
                    "log_line": line_number,
                }
                seen_hashes.add(transaction_hash)
                worker_transactions.append(record)
                transactions.append(record)

        if registration_success_line is None:
            raise RuntimeError(f"No successful RTMR3 registration in {log_path.name}")
        preceding = [
            record
            for record in worker_transactions
            if int(record["log_line"]) < registration_success_line
            and record["operation"] == "contract_transaction"
        ]
        if len(preceding) != 1:
            raise RuntimeError(
                f"Expected one receipt before registration success for {worker}, found {len(preceding)}"
            )
        registrations.append({**preceding[0], "registration_verified_line": registration_success_line})

    if len(registrations) != 6:
        raise RuntimeError(f"Expected six successful RTMR3 registrations, found {len(registrations)}")
    return transactions, registrations


def _worker_activity() -> list[dict[str, object]]:
    snapshot = json.loads((RAW_DIR / "prometheus_final_snapshot.json").read_text())
    results = snapshot["data"]["result"]
    events: dict[str, dict[str, int]] = defaultdict(dict)
    for item in results:
        metric = item["metric"]
        name = metric.get("__name__", "")
        value = float(item["value"][1])
        worker = metric.get("worker")
        if name == "dfl_worker_runtime_event_total" and worker:
            events[worker][metric.get("event", "")] = int(value)
    transactions, registrations = _worker_gas_records()
    gas_by_worker: dict[str, int] = defaultdict(int)
    transactions_by_worker: dict[str, int] = defaultdict(int)
    registration_gas_by_worker = {
        str(record["worker"]): int(record["gas_used"]) for record in registrations
    }
    for record in transactions:
        worker = str(record["worker"])
        gas_by_worker[worker] += int(record["gas_used"])
        transactions_by_worker[worker] += 1

    rows = []
    for worker in sorted(events, key=lambda value: int(value.split("-")[1])):
        rows.append(
            {
                "worker": worker,
                "training_rounds": events[worker].get("worker.training.finished", 0),
                "model_transfers": events[worker].get("worker.model_transfer.finished", 0),
                "aggregator_rounds": events[worker].get("aggregator.global_model_evaluation", 0),
                "selection_gap_recoveries": events[worker].get(
                    "worker.aggregator_selection_gap.recovered", 0
                ),
                "transactions": transactions_by_worker[worker],
                "registration_gas": registration_gas_by_worker[worker],
                "gas_used": gas_by_worker[worker],
            }
        )
    return rows


def _scalar_metrics() -> dict[str, float]:
    snapshot = json.loads((RAW_DIR / "prometheus_final_snapshot.json").read_text())
    wanted = {
        "dfl_aborted_round_attempts",
        "dfl_aggregations_total",
        "dfl_completed_round_count",
        "dfl_contract_init_gas_used_total",
        "dfl_model_transfers_total",
        "dfl_successful_training_rounds",
        "dfl_training_starts_total",
    }
    values: dict[str, float] = {}
    for item in snapshot["data"]["result"]:
        name = item["metric"].get("__name__")
        if name in wanted and len(item["metric"]) <= 3:
            values[name] = float(item["value"][1])
    return values


def _imbalance() -> dict[str, object]:
    labels = np.load(TEST_SPLIT)["labels"].astype(np.uint8)
    positive_rate = float(labels.mean())
    any_positive = labels.any(axis=1)
    return {
        "sample_count": int(labels.shape[0]),
        "label_count": int(labels.shape[1]),
        "positive_label_positions": int(labels.sum()),
        "total_label_positions": int(labels.size),
        "positive_label_rate": positive_rate,
        "positive_label_percent": positive_rate * 100.0,
        "all_negative_label_accuracy_percent": (1.0 - positive_rate) * 100.0,
        "all_negative_exact_match_percent": float((~any_positive).mean() * 100.0),
        "all_negative_macro_f1": 0.0,
        "all_negative_auroc": 0.5,
        "mean_positive_labels_per_sample": float(labels.sum(axis=1).mean()),
        "samples_with_any_positive_percent": float(any_positive.mean() * 100.0),
    }


def _best_row(rows: list[dict[str, object]], metric: str, minimize: bool = False) -> dict[str, object]:
    return (min if minimize else max)(rows, key=lambda row: float(row[metric]))


def main() -> None:
    rows = _parse_worker_logs()
    fields = [
        "experiment_id",
        "dataset",
        "federated_round",
        "global_model_round",
        "timestamp_unix_ms",
        "participant_count",
        "aggregated_model_count",
        "expected_models",
        "aggregator_worker",
        "selected_candidate",
        "output_kind",
        "gate_passed",
        "parent_validation_loss",
        "selected_validation_loss",
        "validation_data_hash",
        "accuracy_percent",
        "exact_match_percent",
        "loss",
        "micro_f1",
        "macro_f1",
        "macro_auroc",
        "sample_count",
        "label_count",
        "correct_predictions",
        "total_predictions",
    ]
    _write_csv(RUN_DIR / "round_metrics.csv", rows, fields)
    _write_csv(
        RUN_DIR / "plot-metrics.csv",
        [
            {
                "round": row["global_model_round"],
                "loss": row["loss"],
                "microf1": row["micro_f1"],
                "macrof1": row["macro_f1"],
                "auroc": row["macro_auroc"],
                "accuracy": row["accuracy_percent"],
                "exactmatch": row["exact_match_percent"],
            }
            for row in rows
        ],
        ["round", "loss", "microf1", "macrof1", "auroc", "accuracy", "exactmatch"],
    )

    activity = _worker_activity()
    activity_fields = [
        "worker",
        "training_rounds",
        "model_transfers",
        "aggregator_rounds",
        "selection_gap_recoveries",
        "transactions",
        "registration_gas",
        "gas_used",
    ]
    _write_csv(RUN_DIR / "worker_activity.csv", activity, activity_fields)

    first = rows[0]
    final = rows[-1]
    metric_rows = []
    for metric, label, minimize in (
        ("accuracy_percent", "Label-wise accuracy (%)", False),
        ("loss", "Unweighted test BCE", True),
        ("macro_auroc", "Macro AUROC", False),
        ("macro_f1", "Macro F1", False),
        ("micro_f1", "Micro F1", False),
        ("exact_match_percent", "Exact match (%)", False),
    ):
        best = _best_row(rows, metric, minimize=minimize)
        metric_rows.append(
            {
                "metric": label,
                "first": first[metric],
                "first_global_model_round": first["global_model_round"],
                "best": best[metric],
                "best_global_model_round": best["global_model_round"],
                "final": final[metric],
                "final_global_model_round": final["global_model_round"],
            }
        )
    _write_csv(
        RUN_DIR / "first_best_final.csv",
        metric_rows,
        [
            "metric",
            "first",
            "first_global_model_round",
            "best",
            "best_global_model_round",
            "final",
            "final_global_model_round",
        ],
    )
    # The journal consumes this table directly through pgfplotstable.  Keep the
    # canonical CSV tool-neutral while escaping percent signs in the TeX-facing
    # copy so a regeneration cannot turn the rest of a row into a TeX comment.
    journal_metric_rows = [
        {**row, "metric": str(row["metric"]).replace("%", r"\%")}
        for row in metric_rows
    ]
    _write_csv(
        ROOT / "journal" / "data" / "authoritative_phala_6w_24r_first_best_final.csv",
        journal_metric_rows,
        [
            "metric",
            "first",
            "first_global_model_round",
            "best",
            "best_global_model_round",
            "final",
            "final_global_model_round",
        ],
    )

    scalar = _scalar_metrics()
    imbalance = _imbalance()
    inference = json.loads((RAW_DIR / "tee_inference_result.json").read_text())
    status = json.loads((RAW_DIR / "runtime_status.json").read_text())
    start_ms = int(first["timestamp_unix_ms"])
    end_ms = int(final["timestamp_unix_ms"])
    init_gas = int(scalar["dfl_contract_init_gas_used_total"])
    worker_transactions = sum(int(row["transactions"]) for row in activity)
    worker_gas = sum(int(row["gas_used"]) for row in activity)
    _, registrations = _worker_gas_records()
    registration_gas = sum(int(row["gas_used"]) for row in registrations)

    _write_csv(
        RUN_DIR / "registration_gas_audit.csv",
        registrations,
        [
            "worker",
            "account",
            "device_id",
            "transaction_hash",
            "operation",
            "block_number",
            "gas_used",
            "log_file",
            "log_line",
            "registration_verified_line",
        ],
    )

    manifest = {
        "run_id": "authoritative-phala-6w-24r-20260830",
        "evidence_policy": "sole evaluation run used by thesis, journal, and presentation",
        "runtime": {
            "mode": "phala",
            "node": "prod9",
            "dstack_version": "0.5.9",
            "worker_count": 6,
            "tier_1_concurrent_tee_limit_observed": 8,
            "infrastructure_tee_count": 2,
            "infrastructure_tees": ["contract/control runtime", "Ollama"],
            "worker0_profile": "training plus TEE inference",
            "worker1_to_5_profile": "training only",
            "worker_image": "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:578e7fe9c5426c2ed92119a26bee4be54b664e93e6aa71bd91bf9af4fdb1b7b4",
            "control_api_image": "ghcr.io/uzhw8rgl/master-thesis-control-api@sha256:92c8b24644084df308cda84c1d28bc2c2f1c8beb86ab9dc1e099733d19e8c350",
            "ui_image": "ghcr.io/uzhw8rgl/master-thesis-ui@sha256:f01a75934d3e6c1b1aa784d1faa80a4f05b1e97264ca509bdd3255f66fe014a7",
            "agent_image": "ghcr.io/uzhw8rgl/master-thesis-agent@sha256:78df895d7aa2637488da069e76a80f6d44842969e301d06e5cae0ce0fb8863ae",
            "smart_contracts_image": "ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:b09465bb1c1dbd54b7ad527e1ec66c9c74463f600ea86e53fa65673501a394f0",
            "run_roster_digest": status["runtime"]["run_roster"]["digest"],
        },
        "training": {
            "dataset": "ChestMNIST",
            "training_samples": 78468,
            "training_shards": 6,
            "samples_per_shard": 13078,
            "model": "two-convolution CNN",
            "optimizer": "AdamW",
            "learning_rate": 0.003,
            "learning_rate_schedule": "constant",
            "weight_decay": 0.0001,
            "positive_class_weight_cap": 10,
            "local_epochs": 2,
            "bootstrap_completions": 1,
            "federated_rounds": 24,
            "client_updates_per_round": 5,
            "successful_federated_rounds": int(scalar["dfl_aggregations_total"]),
            "aborted_round_attempts": int(scalar["dfl_aborted_round_attempts"]),
            "training_starts": int(scalar["dfl_training_starts_total"]),
            "model_transfers": int(scalar["dfl_model_transfers_total"]),
            "first_evaluation_utc": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
            "last_evaluation_utc": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
            "first_to_last_evaluation_seconds": (end_ms - start_ms) / 1000.0,
            "mean_evaluation_interval_seconds": (end_ms - start_ms) / 1000.0 / 23.0,
        },
        "learning": {
            "first": {field: first[field] for field in ("global_model_round", "accuracy_percent", "loss", "macro_auroc", "macro_f1", "micro_f1", "exact_match_percent")},
            "final": {field: final[field] for field in ("global_model_round", "accuracy_percent", "loss", "macro_auroc", "macro_f1", "micro_f1", "exact_match_percent")},
            "best_test_bce": _best_row(rows, "loss", minimize=True)["loss"],
            "best_test_bce_round": _best_row(rows, "loss", minimize=True)["global_model_round"],
            "best_macro_auroc": _best_row(rows, "macro_auroc")["macro_auroc"],
            "best_macro_auroc_round": _best_row(rows, "macro_auroc")["global_model_round"],
            "best_macro_f1": _best_row(rows, "macro_f1")["macro_f1"],
            "best_macro_f1_round": _best_row(rows, "macro_f1")["global_model_round"],
            "interpretation": "Functional distributed-learning evidence, not clinical validation; label-wise accuracy is dominated by negative labels.",
        },
        "imbalance_baselines": imbalance,
        "gas_accounting": {
            "scope": "Anvil EVM protocol gas accounting",
            "source": "deduplicated receipt-derived gas records in the six immutable worker logs",
            "registration": {"transactions": 6, "gas": registration_gas},
            "initialization": {"transactions": 24, "gas": init_gas},
            "worker": {"transactions": worker_transactions, "gas": worker_gas},
            "total": {"transactions": worker_transactions + 24, "gas": init_gas + worker_gas},
        },
        "tee_inference": {
            "job_id": inference["job_id"],
            "sample_index": inference["sample_index"],
            "ground_truth": inference["ground_truth"],
            "predicted_labels": inference["predicted_labels"],
            "model_version": inference["verification"]["model_version"],
            "manifest_sha256": inference["verification"]["manifest_sha256"],
            "model_sha256": "0da5e8fede045cb00f2e75ec0e0e0ca80dfc243e62a3a7b01c03fe4b050d26c8",
            "duration_microseconds": inference["verification"]["duration_microseconds"],
            "quote_bytes": inference["verification"]["quote_bytes"],
            "rtmr3_event_count": inference["verification"]["rtmr3_event_count"],
            "dcap_collateral_verified": inference["verification"]["dcap_collateral_verified"],
            "transparency_transaction": inference["transparency_log"]["transaction_id"],
            "receipt_signature_transaction": inference["transparency_log"]["receipts"][0]["sigtxid"],
            "transparency_record_id": inference["transparency_record_id"],
            "verification_scope": inference["verification"]["verification_scope"],
        },
    }
    (RUN_DIR / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
