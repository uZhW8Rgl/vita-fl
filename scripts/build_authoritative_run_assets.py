#!/usr/bin/env python3
"""Regenerate publication assets from the 2026-09-01 Phala FedAvg run.

The script is deliberately offline. It derives every compact table and the
manifest from the checked-in observability export, final Prometheus scrape,
worker logs, runtime status, TEE-inference result, and local ChestMNIST split.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
RUN_DIR = ROOT / "data" / "evaluation" / "authoritative-phala-6w-24r-20260901"
RAW_DIR = RUN_DIR / "raw"
TEST_SPLIT = ROOT / "data" / "chestmnist" / "test_data" / "test-data.npz"
PUBLICATION_STEMS = {
    "round_metrics.csv": "authoritative_phala_6w_24r.csv",
    "first_best_final.csv": "authoritative_phala_6w_24r_first_best_final.csv",
    "worker_activity.csv": "authoritative_phala_6w_24r_worker_activity.csv",
    "run_manifest.json": "authoritative_phala_6w_24r_manifest.json",
}


def _write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _parse_worker_logs() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    paths = sorted((RAW_DIR / "workers").glob("vita-fl-worker[0-5]-full.log.gz"))
    if len(paths) != 6:
        raise RuntimeError(f"Expected six worker logs, found {len(paths)}")
    for log_path in paths:
        worker_match = re.search(r"worker(\d+)", log_path.name)
        if worker_match is None:
            raise RuntimeError(f"Cannot derive worker identity from {log_path.name}")
        worker = f"VM-{worker_match.group(1)}"
        with gzip.open(log_path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith('{"accuracy_percent"'):
                    continue
                payload = json.loads(line)
                if payload.get("kind") != "global_model_evaluation":
                    continue
                payload.update(
                    {
                        "experiment_id": "phala",
                        "federated_round": int(payload["source_round"]),
                        "global_model_round": int(payload["round"]),
                        "aggregator_worker": worker,
                        "aggregation_rule": "equal_weight_fedavg",
                    }
                )
                records.append(payload)
    records.sort(key=lambda row: int(row["federated_round"]))
    if [int(row["federated_round"]) for row in records] != list(range(1, 25)):
        raise RuntimeError("Expected exactly one evaluation for each federated round 1--24")
    if [int(row["global_model_round"]) for row in records] != list(range(2, 26)):
        raise RuntimeError("Expected global-model rounds 2--25")
    return records


def _read_prometheus() -> list[tuple[str, dict[str, str], float]]:
    samples: list[tuple[str, dict[str, str], float]] = []
    pattern = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+)$')
    label_pattern = re.compile(r'(\w+)="((?:\\.|[^"])*)"')
    for line in (RAW_DIR / "control_metrics.prom").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        labels = {key: value for key, value in label_pattern.findall(match.group(2) or "")}
        samples.append((match.group(1), labels, float(match.group(3))))
    return samples


def _scalar_metrics(samples: list[tuple[str, dict[str, str], float]]) -> dict[str, float]:
    wanted = {
        "dfl_aborted_round_attempts",
        "dfl_aggregations_total",
        "dfl_completed_round_count",
        "dfl_contract_init_gas_used_total",
        "dfl_model_transfers_total",
        "dfl_successful_training_rounds",
        "dfl_training_starts_total",
        "dfl_worker_gas_used_total",
        "dfl_worker_registration_gas_used_total",
        "dfl_worker_registration_receipt_gap",
    }
    values = {name: value for name, labels, value in samples if name in wanted and not labels}
    missing = wanted - values.keys()
    if missing:
        raise RuntimeError(f"Missing Prometheus scalars: {sorted(missing)}")
    return values


def _transaction_rows() -> list[dict[str, str]]:
    with (RAW_DIR / "transaction_costs.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    hashes = [row["transactionHash"].lower() for row in rows]
    if len(rows) != 329 or len(set(hashes)) != 329:
        raise RuntimeError("Expected 329 unique receipt-derived transactions")
    return rows


def _worker_activity(
    samples: list[tuple[str, dict[str, str], float]],
    transactions: list[dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    events: dict[str, dict[str, int]] = defaultdict(dict)
    accounts: dict[str, str] = {}
    for name, labels, value in samples:
        worker = labels.get("worker")
        if name == "dfl_worker_runtime_event_total" and worker:
            events[worker][labels.get("event", "")] = int(value)
            accounts[worker] = labels.get("account", "")

    with (RAW_DIR / "worker_costs_by_worker.csv").open(newline="", encoding="utf-8") as handle:
        costs = {row["worker"]: row for row in csv.DictReader(handle)}

    registrations = []
    for row in transactions:
        if row["scope"] != "worker" or row["operation"] != "rtmr3_registration":
            continue
        worker = f"VM-{int(row['deviceId'])}"
        registrations.append(
            {
                "worker": worker,
                "account": row["account"],
                "device_id": int(row["deviceId"]),
                "transaction_hash": row["transactionHash"].lower(),
                "block_number": int(row["blockNumber"]),
                "gas_used": int(row["gasUsed"]),
            }
        )
    registrations.sort(key=lambda row: int(row["device_id"]))
    if len(registrations) != 6:
        raise RuntimeError(f"Expected six RTMR3 registration receipts, found {len(registrations)}")
    registration_gas = {str(row["worker"]): int(row["gas_used"]) for row in registrations}

    rows: list[dict[str, object]] = []
    for index in range(6):
        worker = f"VM-{index}"
        event = events[worker]
        cost = costs[worker]
        rows.append(
            {
                "worker": worker,
                "account": accounts[worker],
                "training_rounds": event.get("worker.training.finished", 0),
                "model_transfers": event.get("worker.model_transfer.finished", 0),
                "aggregator_rounds": event.get("aggregator.global_model_evaluation", 0),
                "selection_gap_recoveries": event.get("worker.aggregator_selection_gap.recovered", 0),
                "transactions": int(cost["transactionCount"]),
                "registration_gas": registration_gas[worker],
                "gas_used": int(cost["gasUsed"]),
            }
        )
    return rows, registrations


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


def _publish() -> None:
    destinations = [
        WORKSPACE / "journal" / "data",
        WORKSPACE / "overleaf" / "images" / "data",
        WORKSPACE / "presentation" / "data" / "evaluation",
    ]
    for destination in destinations:
        destination.mkdir(parents=True, exist_ok=True)
        for source_name, destination_name in PUBLICATION_STEMS.items():
            source = RUN_DIR / source_name
            target = destination / destination_name
            if destination == WORKSPACE / "journal" / "data" and source_name == "first_best_final.csv":
                with source.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                for row in rows:
                    row["metric"] = row["metric"].replace("%", r"\%")
                _write_csv(target, rows, list(rows[0]))
            else:
                shutil.copyfile(source, target)
    shutil.copyfile(RUN_DIR / "plot-metrics.csv", WORKSPACE / "overleaf" / "images" / "data" / "plot-metrics.csv")


def main() -> None:
    rows = _parse_worker_logs()
    round_fields = [
        "experiment_id", "dataset", "federated_round", "global_model_round",
        "timestamp_unix_ms", "participant_count", "aggregated_model_count",
        "expected_models", "aggregator_worker", "aggregation_rule",
        "accuracy_percent", "exact_match_percent", "loss", "micro_f1",
        "macro_f1", "macro_auroc", "sample_count", "label_count",
        "correct_predictions", "total_predictions",
    ]
    _write_csv(RUN_DIR / "round_metrics.csv", rows, round_fields)
    _write_csv(
        RUN_DIR / "plot-metrics.csv",
        [
            {
                "round": row["global_model_round"], "loss": row["loss"],
                "microf1": row["micro_f1"], "macrof1": row["macro_f1"],
                "auroc": row["macro_auroc"], "accuracy": row["accuracy_percent"],
                "exactmatch": row["exact_match_percent"],
            }
            for row in rows
        ],
        ["round", "loss", "microf1", "macrof1", "auroc", "accuracy", "exactmatch"],
    )

    first, final = rows[0], rows[-1]
    metric_rows = []
    for metric, label, minimize in (
        ("accuracy_percent", "Label-wise accuracy (%)", False),
        ("loss", "Unweighted test BCE", True),
        ("macro_auroc", "Macro AUROC", False),
        ("macro_f1", "Macro F1", False),
        ("micro_f1", "Micro F1", False),
        ("exact_match_percent", "Exact match (%)", False),
    ):
        best = _best_row(rows, metric, minimize)
        metric_rows.append(
            {
                "metric": label, "first": first[metric],
                "first_global_model_round": first["global_model_round"],
                "best": best[metric], "best_global_model_round": best["global_model_round"],
                "final": final[metric], "final_global_model_round": final["global_model_round"],
            }
        )
    _write_csv(
        RUN_DIR / "first_best_final.csv", metric_rows,
        ["metric", "first", "first_global_model_round", "best", "best_global_model_round", "final", "final_global_model_round"],
    )

    samples = _read_prometheus()
    scalar = _scalar_metrics(samples)
    transactions = _transaction_rows()
    activity, registrations = _worker_activity(samples, transactions)
    activity_fields = ["worker", "account", "training_rounds", "model_transfers", "aggregator_rounds", "selection_gap_recoveries", "transactions", "registration_gas", "gas_used"]
    _write_csv(RUN_DIR / "worker_activity.csv", activity, activity_fields)
    _write_csv(RUN_DIR / "registration_gas_audit.csv", registrations, ["worker", "account", "device_id", "transaction_hash", "block_number", "gas_used"])

    status = json.loads((RAW_DIR / "runtime_status.json").read_text(encoding="utf-8"))
    inference = json.loads((RAW_DIR / "tee_inference_result.json").read_text(encoding="utf-8"))
    transparency = json.loads((RAW_DIR / "transparency_records.json").read_text(encoding="utf-8"))
    start_ms, end_ms = int(first["timestamp_unix_ms"]), int(final["timestamp_unix_ms"])
    worker_gas = sum(int(row["gas_used"]) for row in activity)
    worker_transactions = sum(int(row["transactions"]) for row in activity)
    registration_gas = sum(int(row["gas_used"]) for row in registrations)
    init_transactions = [row for row in transactions if row["scope"] == "smart_contracts_init"]
    init_gas = sum(int(row["gasUsed"]) for row in init_transactions)
    if worker_gas != int(scalar["dfl_worker_gas_used_total"]):
        raise RuntimeError("Worker gas does not match the final Prometheus total")
    if registration_gas != int(scalar["dfl_worker_registration_gas_used_total"]):
        raise RuntimeError("Registration gas does not match the final Prometheus total")

    learning_fields = ("global_model_round", "accuracy_percent", "loss", "macro_auroc", "macro_f1", "micro_f1", "exact_match_percent")
    manifest = {
        "run_id": "authoritative-phala-6w-24r-20260901",
        "evidence_policy": "sole evaluation run used by thesis, journal, and presentation",
        "runtime": {
            "mode": "phala", "node": "prod9", "dstack_version": "0.5.9",
            "worker_count": 6, "tier_1_concurrent_tee_limit_observed": 8,
            "infrastructure_tee_count": 2,
            "infrastructure_tees": ["contract/control runtime", "Ollama"],
            "worker0_profile": "training plus TEE inference",
            "worker1_to_5_profile": "training only",
            "worker_image": "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:e0b3771ca6135932405054947a4eeca88d4c7612d91f93cf2f0482eb804a1b27",
            "control_api_image": "ghcr.io/uzhw8rgl/master-thesis-control-api@sha256:1371e015c4b8e6042ab1094501bcdc00d283c576b2880e6492de67a88037b9cf",
            "ui_image": "ghcr.io/uzhw8rgl/master-thesis-ui@sha256:f01a75934d3e6c1b1aa784d1faa80a4f05b1e97264ca509bdd3255f66fe014a7",
            "agent_image": "ghcr.io/uzhw8rgl/master-thesis-agent@sha256:b6520d0a970362e77a420621acf476cca2afb56fdad3bca54d20afabbcdbb6c5",
            "smart_contracts_image": "ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:1ed6bedbe8afd7b2ee433ca7097667c624a415b8fe7bb37c73e0536bda9b16e7",
            "zk_inference_image": "ghcr.io/uzhw8rgl/master-thesis-zk-inference@sha256:2d25f9c1aca15616ce1a45d3b64a29fec10f3e44a57dfea18c55e9fd71e25568",
            "run_roster_digest": status["runtime"]["run_roster"]["digest"],
        },
        "training": {
            "dataset": "ChestMNIST", "training_samples": 78468,
            "training_shards": 6, "samples_per_shard": 13078,
            "model": "compact two-convolution CNN", "aggregation": "deterministic equal-weight FedAvg",
            "optimizer": "AdamW", "learning_rate": 0.003,
            "learning_rate_schedule": "constant", "weight_decay": 0.0001,
            "positive_class_weight_cap": 10, "local_epochs": 2,
            "bootstrap_completions": 1, "federated_rounds": 24,
            "client_updates_per_round": 5,
            "successful_federated_rounds": int(scalar["dfl_aggregations_total"]),
            "aborted_round_attempts": int(scalar["dfl_aborted_round_attempts"]),
            "training_starts": int(scalar["dfl_training_starts_total"]),
            "model_transfers": int(scalar["dfl_model_transfers_total"]),
            "first_evaluation_utc": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
            "last_evaluation_utc": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
            "first_to_last_evaluation_seconds": (end_ms - start_ms) / 1000,
            "mean_evaluation_interval_seconds": (end_ms - start_ms) / 1000 / 23,
        },
        "learning": {
            "first": {field: first[field] for field in learning_fields},
            "final": {field: final[field] for field in learning_fields},
            "best_test_bce": _best_row(rows, "loss", True)["loss"],
            "best_test_bce_round": _best_row(rows, "loss", True)["global_model_round"],
            "best_macro_auroc": _best_row(rows, "macro_auroc")["macro_auroc"],
            "best_macro_auroc_round": _best_row(rows, "macro_auroc")["global_model_round"],
            "best_macro_f1": _best_row(rows, "macro_f1")["macro_f1"],
            "best_macro_f1_round": _best_row(rows, "macro_f1")["global_model_round"],
            "interpretation": "Functional distributed-learning evidence, not clinical validation; label-wise accuracy is dominated by negative labels.",
        },
        "imbalance_baselines": _imbalance(),
        "gas_accounting": {
            "scope": "Anvil EVM protocol gas accounting",
            "source": "329 unique receipt-derived rows in the observability export",
            "registration": {"transactions": 6, "gas": registration_gas},
            "initialization": {"transactions": len(init_transactions), "gas": init_gas},
            "worker": {"transactions": worker_transactions, "gas": worker_gas},
            "total": {"transactions": len(transactions), "gas": sum(int(row["gasUsed"]) for row in transactions)},
        },
        "tee_inference": {
            "job_id": inference["job_id"], "sample_index": inference["sample_index"],
            "ground_truth": inference["ground_truth"], "predicted_labels": inference["predicted_labels"],
            "model_version": inference["verification"]["model_version"],
            "manifest_sha256": inference["verification"]["manifest_sha256"],
            "model_sha256": "bf72fa9369459804f6c82caa4d6f922e7c2cea170f0fa8ed8d044b4bd6383c80",
            "duration_microseconds": inference["verification"]["duration_microseconds"],
            "quote_bytes": inference["verification"]["quote_bytes"],
            "rtmr3_event_count": inference["verification"]["rtmr3_event_count"],
            "dcap_collateral_verified": inference["verification"]["dcap_collateral_verified"],
            "transparency_transaction": inference["transparency_log"]["transaction_id"],
            "receipt_signature_transaction": inference["transparency_log"]["receipts"][0]["sigtxid"],
            "transparency_record_id": inference["transparency_record_id"],
            "verification_scope": inference["verification"]["verification_scope"],
        },
        "transparency": {
            "exported_record_count": len(transparency["records"]),
            "retained": ["agent session", "TEE result", "record read-models and digests"],
            "not_retained": ["evidence.cbor bytes", "transparent-statement.cose bytes"],
        },
    }
    (RUN_DIR / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _publish()


if __name__ == "__main__":
    main()
