#!/usr/bin/env python3
"""Small control API for contract initialization and training restarts."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

WORKSPACE_ROOT = Path(os.environ.get("TRAINING_WORKSPACE_ROOT", "/workspace")).resolve()
TRAINING_ENV_FILE = Path(
    os.environ.get("TRAINING_CONFIG_FILE", str(WORKSPACE_ROOT / ".env"))
).resolve()
TRAINING_COMPOSE_FILE = WORKSPACE_ROOT / "compose.yml"
EVALUATION_SUMMARY_CSV = WORKSPACE_ROOT / "data" / "evaluation" / "global_model_round_summary.csv"
TRANSACTION_COST_CSV = WORKSPACE_ROOT / "data" / "evaluation" / "transaction_costs.csv"
CONTROL_API_STARTED_AT_UNIX_MS = int(time.time() * 1000)
CONTROL_RUNTIME_MODE = os.environ.get("CONTROL_RUNTIME_MODE", "local").strip().lower()
TRAINING_CONFIG_KEYS = ("ROUND", "EPOCH", "WORKER_COUNT", "CLIENT_LIMIT")
CONTRACT_TIMEOUT_SECONDS = 600
OBSERVABILITY_VOLUME_NAMES = (
    "grafana-data",
    "prometheus-data",
)
OBSERVABILITY_SERVICES = [
    "grafana",
    "prometheus",
]
EVALUATION_ARTIFACT_PATTERNS = (
    "global_model_round_summary.csv",
    "global_model_round_summary.jsonl",
    "global_model_label_metrics.csv",
    "global_model_sample_metrics.csv",
    "transaction_costs.csv",
)
STATIC_CONTAINER_NAMES = {
    "anvil": "anvil",
    "ipfs": "ipfs_local",
    "smart-contracts": "smart-contracts",
    "agent": "agent",
    "zk-inference": "zk-inference",
}

app = FastAPI(title="Master Thesis Control API")
operation_lock = asyncio.Lock()
_phala_worker_controller: Any | None = None
_telemetry_lock = threading.Lock()
_telemetry_records: list[dict[str, Any]] = []
_telemetry_nonces: dict[str, int] = {}
TELEMETRY_MAX_RECORDS = 20_000
TELEMETRY_MAX_AGE_MS = 10 * 60 * 1000


def phala_runtime_mode() -> bool:
    return CONTROL_RUNTIME_MODE == "phala"


def environment_flag(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return os.environ.get(name, fallback).strip().lower() in {"1", "true", "yes", "on"}


def phala_worker_controller():
    global _phala_worker_controller
    if not phala_runtime_mode():
        raise RuntimeError("dynamic Phala workers are only available in CONTROL_RUNTIME_MODE=phala")
    if _phala_worker_controller is None:
        from control_api.phala_workers import controller_from_environment

        _phala_worker_controller = controller_from_environment()
    return _phala_worker_controller


def require_control_admin(request: Request) -> None:
    configured = os.environ.get("CONTROL_ADMIN_TOKEN", "").strip()
    if not configured:
        raise HTTPException(status_code=503, detail="CONTROL_ADMIN_TOKEN is not configured")
    supplied = request.headers.get("x-control-admin-token", "").strip()
    authorization = request.headers.get("authorization", "").strip()
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="invalid control administrator token")


def resolve_docker_bin() -> str:
    configured = os.environ.get("TRAINING_DOCKER_BIN", "").strip()
    candidates = [
        configured,
        shutil.which("docker") or "",
        "/usr/bin/docker",
        "/usr/local/bin/docker",
        "/bin/docker",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "Docker CLI was not found in the control-api container. Rebuild control-api or set TRAINING_DOCKER_BIN."
    )


def _env_line_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*(?:#.*)?$", line)
    if not match:
        return None
    return match.group(1), match.group(2)


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _available_worker_services(compose_file: Path = TRAINING_COMPOSE_FILE) -> list[str]:
    if not compose_file.exists():
        return []

    services: list[tuple[int, str]] = []
    for line in compose_file.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s{2}(VM-(\d+)):\s*$", line)
        if match:
            services.append((int(match.group(2)), match.group(1)))
    services.sort(key=lambda item: item[0])
    return [name for _, name in services]


def read_env_values(env_file: Path = TRAINING_ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            parsed = _env_line_value(line)
            if parsed is None:
                continue
            key, value = parsed
            values[key] = value.strip()
    return values


def _dynamic_worker_slots() -> dict[str, str]:
    try:
        inventory = json.loads(os.environ.get("DYNAMIC_WORKER_INVENTORY", "[]"))
    except json.JSONDecodeError:
        return {}
    if isinstance(inventory, dict):
        records = list(inventory.values())
    elif isinstance(inventory, list):
        records = inventory
    else:
        return {}
    slots: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        address = str(record.get("account_address", "")).strip().lower()
        if re.fullmatch(r"0x[a-f0-9]{40}", address):
            slots[address] = str(record.get("slot", ""))
    return slots


def _canonical_telemetry_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_telemetry(payload: dict[str, Any], signature: str) -> None:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    required = {"version", "account", "device_id", "event", "attributes", "timestamp_unix_ms", "nonce"}
    if set(payload) != required or payload.get("version") != 1:
        raise ValueError("invalid telemetry payload schema")
    account = str(payload.get("account", "")).strip().lower()
    device_id = str(payload.get("device_id", "")).strip()
    event = str(payload.get("event", "")).strip()
    nonce = str(payload.get("nonce", "")).strip()
    attributes = payload.get("attributes")
    try:
        timestamp_unix_ms = int(payload.get("timestamp_unix_ms"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid telemetry timestamp") from exc
    configured_workers = _dynamic_worker_slots()
    if account not in configured_workers:
        raise ValueError("telemetry account is not in the configured worker inventory")
    if not secrets.compare_digest(device_id, configured_workers[account]):
        raise ValueError("telemetry device ID does not match account")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", event):
        raise ValueError("invalid telemetry event name")
    if not isinstance(attributes, dict) or len(_canonical_telemetry_payload(attributes).encode()) > 32_768:
        raise ValueError("invalid telemetry attributes")
    if not re.fullmatch(r"[A-Za-z0-9-]{16,128}", nonce):
        raise ValueError("invalid telemetry nonce")
    now_ms = int(time.time() * 1000)
    if abs(now_ms - timestamp_unix_ms) > TELEMETRY_MAX_AGE_MS:
        raise ValueError("telemetry timestamp is outside the accepted window")
    canonical = _canonical_telemetry_payload(payload)
    try:
        recovered = Account.recover_message(encode_defunct(text=canonical), signature=signature).lower()
    except Exception as exc:
        raise ValueError("invalid telemetry signature") from exc
    if not secrets.compare_digest(recovered, account):
        raise ValueError("telemetry signature does not match account")
    with _telemetry_lock:
        cutoff = now_ms - TELEMETRY_MAX_AGE_MS
        expired = [seen_nonce for seen_nonce, seen_at in _telemetry_nonces.items() if seen_at < cutoff]
        for seen_nonce in expired:
            _telemetry_nonces.pop(seen_nonce, None)
        if nonce in _telemetry_nonces:
            raise ValueError("telemetry nonce was already used")
        _telemetry_nonces[nonce] = timestamp_unix_ms
        _telemetry_records.append(dict(payload))
        if len(_telemetry_records) > TELEMETRY_MAX_RECORDS:
            del _telemetry_records[: len(_telemetry_records) - TELEMETRY_MAX_RECORDS]


def _telemetry_snapshot() -> list[dict[str, Any]]:
    with _telemetry_lock:
        return [dict(record) for record in _telemetry_records]


def reset_runtime_telemetry() -> None:
    with _telemetry_lock:
        _telemetry_records.clear()
        _telemetry_nonces.clear()


def telemetry_evaluation_records(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    evaluations: list[dict[str, str]] = []
    for record in records:
        if record.get("event") != "aggregator.global_model_evaluation":
            continue
        attributes = record.get("attributes") or {}
        evaluations.append(
            {
                "experiment_id": "phala",
                "dataset": os.environ.get("DATASET_NAME", "unknown"),
                "round": str(attributes.get("global_model_round", "")),
                "source_round": str(attributes.get("source_round", "")),
                "timestamp_unix_ms": str(record.get("timestamp_unix_ms", "")),
                **{str(key): str(value) for key, value in attributes.items()},
            }
        )
    return evaluations


def telemetry_transaction_cost_records(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    costs: list[dict[str, str]] = []
    for record in records:
        if record.get("event") != "worker.transaction_cost":
            continue
        attributes = record.get("attributes") or {}
        costs.append({str(key): str(value) for key, value in attributes.items()})
    return costs


def telemetry_run_totals(records: list[dict[str, Any]]) -> dict[str, float]:
    event_metrics = {
        "worker.training.started": "dfl_training_starts_total",
        "worker.model_transfer.finished": "dfl_model_transfers_total",
        "aggregator.aggregation.finished": "dfl_aggregations_total",
    }
    totals = {metric_name: 0.0 for metric_name in event_metrics.values()}
    for record in records:
        metric_name = event_metrics.get(str(record.get("event", "")))
        if metric_name:
            totals[metric_name] += 1.0
    return totals


def _prometheus_label_value(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prometheus_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = float(text)
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            return None
        return parsed
    except (TypeError, ValueError):
        return None


def read_evaluation_summary_records(
    path: Path = EVALUATION_SUMMARY_CSV,
    min_timestamp_unix_ms: int | None = None,
) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))
    if min_timestamp_unix_ms is None:
        return records
    fresh_records: list[dict[str, str]] = []
    for record in records:
        timestamp = _prometheus_float(record.get("timestamp_unix_ms"))
        if timestamp is not None and timestamp >= min_timestamp_unix_ms:
            fresh_records.append(record)
    return fresh_records


def read_transaction_cost_records(path: Path = TRANSACTION_COST_CSV) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))

    deduplicated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        transaction_hash = (record.get("transactionHash") or "").strip()
        scope = (record.get("scope") or "").strip()
        if scope == "scope" or transaction_hash == "transactionHash":
            continue
        key = (scope, transaction_hash or f"row-{index}")
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    return deduplicated


def _worker_display_name(device_id: str, account: str) -> str:
    device_id = (device_id or "").strip()
    if device_id:
        if device_id.upper().startswith("VM-"):
            return device_id.upper()
        if device_id.isdigit():
            return f"VM-{device_id}"
        return device_id
    account = (account or "").strip()
    if account and account != "unknown":
        return account
    return "unknown"


def _evaluation_labels(record: dict[str, str]) -> str:
    labels = {
        "experiment_id": record.get("experiment_id", "local"),
        "dataset": record.get("dataset", "unknown"),
        "round": record.get("round", ""),
        "recorded_at_unix_ms": record.get("timestamp_unix_ms", ""),
        "participant_count": record.get("participant_count", ""),
        "aggregated_model_count": record.get("aggregated_model_count", ""),
    }
    return ",".join(f'{key}="{_prometheus_label_value(value)}"' for key, value in labels.items())


def append_evaluation_metrics(
    payload_lines: list[str],
    records: list[dict[str, str]],
    run_totals_override: dict[str, float] | None = None,
) -> None:
    payload_lines.extend(
        [
            (
                "# HELP dfl_global_model_evaluation_info Global model evaluation metadata, "
                "one time series per evaluated round."
            ),
            "# TYPE dfl_global_model_evaluation_info gauge",
        ]
    )
    for record in records:
        info_labels = {
            "experiment_id": record.get("experiment_id", "local"),
            "dataset": record.get("dataset", "unknown"),
            "round": record.get("round", ""),
            "source_round": record.get("source_round", ""),
            "recorded_at_unix_ms": record.get("timestamp_unix_ms", ""),
            "participant_count": record.get("participant_count", ""),
            "aggregated_model_count": record.get("aggregated_model_count", ""),
            "expected_models": record.get("expected_models", ""),
            "sample_count": record.get("sample_count", ""),
            "label_count": record.get("label_count", ""),
            "accuracy_percent": record.get("accuracy_percent", ""),
            "loss": record.get("loss", ""),
            "micro_f1": record.get("micro_f1", ""),
            "macro_f1": record.get("macro_f1", ""),
            "macro_auroc": record.get("macro_auroc", ""),
            "exact_match_percent": record.get("exact_match_percent", ""),
        }
        labels = ",".join(f'{key}="{_prometheus_label_value(value)}"' for key, value in info_labels.items())
        payload_lines.append(f"dfl_global_model_evaluation_info{{{labels}}} 1")

    metric_map = {
        "expected_models": "dfl_global_model_evaluation_expected_models",
        "aggregated_model_count": "dfl_global_model_evaluation_aggregated_model_count",
        "accuracy_percent": "dfl_global_model_evaluation_accuracy_percent",
        "loss": "dfl_global_model_evaluation_loss",
        "micro_f1": "dfl_global_model_evaluation_micro_f1",
        "macro_f1": "dfl_global_model_evaluation_macro_f1",
        "macro_auroc": "dfl_global_model_evaluation_macro_auroc",
        "exact_match_percent": "dfl_global_model_evaluation_exact_match_percent",
    }
    for field, metric_name in metric_map.items():
        payload_lines.extend(
            [
                "",
                f"# HELP {metric_name} Global model evaluation field '{field}' by evaluated round.",
                f"# TYPE {metric_name} gauge",
            ]
        )
        for record in records:
            value = _prometheus_float(record.get(field))
            if value is None:
                continue
            payload_lines.append(f"{metric_name}{{{_evaluation_labels(record)}}} {value}")

    expected_training_starts = 0.0
    aggregated_model_count = 0.0
    for record in records:
        expected_training_starts += _prometheus_float(record.get("expected_models")) or 0.0
        aggregated_model_count += _prometheus_float(record.get("aggregated_model_count")) or 0.0

    run_totals = run_totals_override or {
        "dfl_training_starts_total": expected_training_starts,
        "dfl_model_transfers_total": aggregated_model_count,
        "dfl_aggregations_total": float(len(records)),
    }
    for metric_name, value in run_totals.items():
        payload_lines.extend(
            [
                "",
                (
                    f"# HELP {metric_name} Current training-run total derived from one "
                    "evaluation record per aggregated round."
                ),
                f"# TYPE {metric_name} gauge",
                f"{metric_name} {value}",
            ]
        )

    if records:
        latest = records[-1]
        latest_round = _prometheus_float(latest.get("round"))
        latest_accuracy = _prometheus_float(latest.get("accuracy_percent"))
        latest_loss = _prometheus_float(latest.get("loss"))
        latest_macro_f1 = _prometheus_float(latest.get("macro_f1"))
        latest_macro_auroc = _prometheus_float(latest.get("macro_auroc"))
        latest_values = {
            "dfl_global_model_latest_round": latest_round,
            "dfl_global_model_latest_accuracy_percent": latest_accuracy,
            "dfl_global_model_latest_loss": latest_loss,
            "dfl_global_model_latest_macro_f1": latest_macro_f1,
            "dfl_global_model_latest_macro_auroc": latest_macro_auroc,
        }
        for metric_name, value in latest_values.items():
            if value is None:
                continue
            payload_lines.extend(
                [
                    "",
                    f"# HELP {metric_name} Latest global model evaluation value mirrored from the round summary CSV.",
                    f"# TYPE {metric_name} gauge",
                    f"{metric_name} {value}",
                ]
            )


def append_transaction_cost_metrics(payload_lines: list[str], records: list[dict[str, str]]) -> None:
    totals: dict[str, dict[str, float]] = {}
    worker_totals: dict[tuple[str, str, str], dict[str, float]] = {}
    for record in records:
        scope = record.get("scope") or "unknown"
        scope_totals = totals.setdefault(scope, {"eth": 0.0, "eur": 0.0, "transactions": 0.0})
        cost_eth = _prometheus_float(record.get("costEth")) or 0.0
        cost_eur = _prometheus_float(record.get("costEur")) or 0.0
        scope_totals["eth"] += cost_eth
        scope_totals["eur"] += cost_eur
        scope_totals["transactions"] += 1.0
        if scope == "worker":
            account = record.get("account") or record.get("from") or "unknown"
            device_id = record.get("deviceId") or "unknown"
            worker_key = (_worker_display_name(device_id, account), account, device_id)
            worker_values = worker_totals.setdefault(worker_key, {"eth": 0.0, "eur": 0.0, "transactions": 0.0})
            worker_values["eth"] += cost_eth
            worker_values["eur"] += cost_eur
            worker_values["transactions"] += 1.0

    metric_specs = {
        "dfl_transaction_cost_eth_total": ("eth", "Total transaction cost in ETH by scope."),
        "dfl_transaction_cost_eur_total": ("eur", "Total transaction cost in EUR by scope."),
        "dfl_transaction_count_total": ("transactions", "Total number of unique transactions by scope."),
    }
    for metric_name, (field, help_text) in metric_specs.items():
        payload_lines.extend(
            [
                "",
                f"# HELP {metric_name} {help_text}",
                f"# TYPE {metric_name} gauge",
            ]
        )
        for scope, values in totals.items():
            payload_lines.append(f'{metric_name}{{scope="{_prometheus_label_value(scope)}"}} {values[field]}')

    worker = totals.get("worker", {})
    contract_init = totals.get("smart_contracts_init", {})
    fixed_totals = {
        "dfl_worker_cost_eth_total": worker.get("eth", 0.0),
        "dfl_worker_cost_eur_total": worker.get("eur", 0.0),
        "dfl_contract_init_cost_eth_total": contract_init.get("eth", 0.0),
        "dfl_contract_init_cost_eur_total": contract_init.get("eur", 0.0),
    }
    for metric_name, value in fixed_totals.items():
        payload_lines.extend(
            [
                "",
                f"# HELP {metric_name} Transaction cost total derived from deduplicated CSV records.",
                f"# TYPE {metric_name} gauge",
                f"{metric_name} {value}",
            ]
        )

    worker_metric_specs = {
        "dfl_worker_cost_eth_by_worker": ("eth", "Worker transaction cost in ETH by worker."),
        "dfl_worker_cost_eur_by_worker": ("eur", "Worker transaction cost in EUR by worker."),
        "dfl_worker_transaction_count_by_worker": ("transactions", "Worker transaction count by worker."),
    }
    for metric_name, (field, help_text) in worker_metric_specs.items():
        payload_lines.extend(
            [
                "",
                f"# HELP {metric_name} {help_text}",
                f"# TYPE {metric_name} gauge",
            ]
        )
        for (worker, account, device_id), values in worker_totals.items():
            labels = (
                f'worker="{_prometheus_label_value(worker)}",'
                f'account="{_prometheus_label_value(account)}",'
                f'device_id="{_prometheus_label_value(device_id)}"'
            )
            payload_lines.append(f"{metric_name}{{{labels}}} {values[field]}")


def read_training_config(
    env_file: Path = TRAINING_ENV_FILE, compose_file: Path = TRAINING_COMPOSE_FILE
) -> dict[str, Any]:
    values = read_env_values(env_file)

    if phala_runtime_mode():
        maximum = max(1, min(_safe_int(os.environ.get("MAX_DYNAMIC_WORKERS"), 20), 20))
        worker_count = max(1, min(_safe_int(os.environ.get("WORKER_COUNT"), 3), maximum))
        client_limit = max(
            1,
            min(_safe_int(os.environ.get("CLIENT_LIMIT"), max(1, worker_count - 1)), max(1, worker_count - 1)),
        )
        return {
            "rounds": max(1, _safe_int(os.environ.get("ROUND"), 5)),
            "epoch": max(1, _safe_int(os.environ.get("EPOCH"), 1)),
            "worker_count": worker_count,
            "client_limit": client_limit,
            "max_worker_count": maximum,
            "available_workers": [f"worker{slot}" for slot in range(maximum)],
        }

    available_workers = _available_worker_services(compose_file)
    max_worker_count = len(available_workers)
    worker_count_default = max_worker_count if max_worker_count > 0 else 1
    worker_count = _safe_int(values.get("WORKER_COUNT"), worker_count_default)
    if max_worker_count > 0:
        worker_count = max(1, min(worker_count, max_worker_count))
    else:
        worker_count = max(1, worker_count)
    max_client_limit = max(1, worker_count - 1)
    client_limit = max(1, min(_safe_int(values.get("CLIENT_LIMIT"), max_client_limit), max_client_limit))

    return {
        "rounds": max(1, _safe_int(values.get("ROUND"), 5)),
        "epoch": max(1, _safe_int(values.get("EPOCH"), 1)),
        "worker_count": worker_count,
        "client_limit": client_limit,
        "max_worker_count": max_worker_count,
        "available_workers": available_workers,
    }


def normalize_training_config(payload: dict[str, Any]) -> dict[str, int]:
    current = read_training_config()
    max_worker_count = max(1, int(current["max_worker_count"]) or 1)

    rounds = max(1, _safe_int(payload.get("rounds"), int(current["rounds"])))
    epoch = max(1, _safe_int(payload.get("epoch"), int(current["epoch"])))
    worker_count = max(1, min(_safe_int(payload.get("worker_count"), int(current["worker_count"])), max_worker_count))
    max_client_limit = max(1, worker_count - 1)
    client_limit = max(1, min(_safe_int(payload.get("client_limit"), int(current["client_limit"])), max_client_limit))

    return {
        "rounds": rounds,
        "epoch": epoch,
        "worker_count": worker_count,
        "client_limit": client_limit,
    }


def phala_worker_training_config(config: dict[str, int]) -> dict[str, int]:
    """Translate user-visible training rounds to the worker's absolute target round.

    Contract round 0 only republishes the bootstrap model; it is not a federated
    client-training round. Therefore N requested training rounds end at round N+1.
    """
    return {**config, "rounds": config["rounds"] + 1}


def write_training_config(config: dict[str, int], env_file: Path = TRAINING_ENV_FILE) -> dict[str, Any]:
    existing_lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    updated_lines: list[str] = []
    seen: set[str] = set()

    value_map = {
        "ROUND": str(config["rounds"]),
        "EPOCH": str(config["epoch"]),
        "WORKER_COUNT": str(config["worker_count"]),
        "CLIENT_LIMIT": str(config["client_limit"]),
    }

    for line in existing_lines:
        parsed = _env_line_value(line)
        if parsed is None:
            updated_lines.append(line)
            continue
        key, _ = parsed
        if key in value_map:
            updated_lines.append(f"{key}={value_map[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)

    if updated_lines and updated_lines[-1] != "":
        updated_lines.append("")

    for key in TRAINING_CONFIG_KEYS:
        if key not in seen:
            updated_lines.append(f"{key}={value_map[key]}")

    env_file.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return read_training_config(env_file)


async def run_subprocess(command: list[str], *, cwd: Path, check: bool = True) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    result = {
        "command": command,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if check and process.returncode != 0:
        raise RuntimeError(
            f"Command failed ({process.returncode}): {' '.join(command)}\n{stderr.strip() or stdout.strip()}"
        )
    return result


def compose_command(*args: str, include_profile: bool = True) -> list[str]:
    command = [
        os.environ.get("TRAINING_COMPOSE_BIN", "docker-compose"),
        "--project-directory",
        str(WORKSPACE_ROOT),
        "-f",
        str(TRAINING_COMPOSE_FILE),
    ]
    compose_project_name = os.environ.get("TRAINING_COMPOSE_PROJECT_NAME", "").strip()
    if compose_project_name:
        command.extend(["-p", compose_project_name])
    compose_profile = os.environ.get("TRAINING_COMPOSE_PROFILE", "runtime").strip()
    if include_profile and compose_profile:
        command.extend(["--profile", compose_profile])
    command.extend(args)
    return command


def compose_project_name() -> str:
    configured = os.environ.get("TRAINING_COMPOSE_PROJECT_NAME", "").strip()
    if configured:
        return configured
    return WORKSPACE_ROOT.name


def use_local_ollama() -> bool:
    return os.environ.get("USE_LOCAL_OLLAMA", "0").strip().lower() in {"1", "true", "yes", "on"}


def compose_ps_state(service_name: str, output: str, container_id: str = "") -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    data_lines = [line for line in lines if not line.lower().startswith(("name ", "name\t"))]
    text = "\n".join(data_lines).strip()
    if not text:
        return missing_service_state(service_name, container_id)

    lowered = text.lower()
    status = "unknown"
    running = False
    exit_code = None

    exited_match = re.search(r"(?:exit|exited)\s*\(?(\d+)\)?", lowered)
    if exited_match:
        status = "exited"
        running = False
        exit_code = int(exited_match.group(1))
    elif "restarting" in lowered:
        status = "restarting"
    elif "up" in lowered or "running" in lowered:
        status = "running"
        running = True
    elif "created" in lowered:
        status = "created"

    return {
        "service": service_name,
        "exists": True,
        "status": status,
        "running": running,
        "exit_code": exit_code,
        "health": None,
        "container_id": container_id or None,
        "container_name": STATIC_CONTAINER_NAMES.get(service_name),
    }


def missing_service_state(service_name: str, container_id: str = "") -> dict[str, Any]:
    return {
        "service": service_name,
        "exists": False,
        "status": "missing",
        "running": False,
        "exit_code": None,
        "health": None,
        "container_id": container_id or None,
        "container_name": STATIC_CONTAINER_NAMES.get(service_name),
    }


def _parse_compose_ps_json(output: str) -> list[dict[str, Any]]:
    text = output.strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
        if isinstance(payload, list):
            return [record for record in payload if isinstance(record, dict)]
        if isinstance(payload, dict):
            return [payload]
    except json.JSONDecodeError:
        pass

    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _compose_json_state(service_name: str, record: dict[str, Any]) -> dict[str, Any]:
    status_text = str(record.get("Status") or "")
    lowered_status = status_text.lower()
    status = str(record.get("State") or "unknown").lower()
    if "exited" in lowered_status:
        status = "exited"
    elif "restarting" in lowered_status:
        status = "restarting"
    elif "up" in lowered_status or status == "running":
        status = "running"

    exit_code = record.get("ExitCode")
    try:
        exit_code = int(exit_code) if exit_code not in {None, ""} else None
    except (TypeError, ValueError):
        exit_code = None

    return {
        "service": service_name,
        "exists": True,
        "status": status,
        "running": status == "running",
        "exit_code": exit_code,
        "health": record.get("Health") or None,
        "container_id": record.get("ID") or None,
        "container_name": record.get("Name") or record.get("Names") or STATIC_CONTAINER_NAMES.get(service_name),
    }


async def compose_ps_state_map(service_names: list[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    ps_result = await run_subprocess(
        compose_command("ps", "-a", "--format", "json", include_profile=False),
        cwd=WORKSPACE_ROOT,
        check=False,
    )
    if ps_result["returncode"] != 0:
        return {}, False

    wanted = set(service_names)
    states: dict[str, dict[str, Any]] = {}
    for record in _parse_compose_ps_json(ps_result["stdout"]):
        service_name = str(record.get("Service") or "")
        if service_name in wanted:
            states[service_name] = _compose_json_state(service_name, record)
    return states, True


async def inspect_worker_services(worker_services: list[str]) -> list[dict[str, Any]]:
    if not worker_services:
        return []

    worker_state_map, json_available = await compose_ps_state_map(worker_services)
    if json_available:
        return [worker_state_map.get(service) or missing_service_state(service) for service in worker_services]

    semaphore = asyncio.Semaphore(32)

    async def inspect_with_limit(service_name: str) -> dict[str, Any]:
        async with semaphore:
            return await inspect_service(service_name)

    return list(await asyncio.gather(*(inspect_with_limit(service) for service in worker_services)))


def _post_json(url: str, payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def probe_anvil_ready(env_values: dict[str, str]) -> bool:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    payload = {"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1}
    response = _post_json(rpc_url, payload)
    return bool(response and response.get("result"))


def probe_ipfs_ready(env_values: dict[str, str]) -> bool:
    kubo_api = env_values.get("KUBO_API", "http://ipfs:5001").strip()
    try:
        request = urllib.request.Request(f"{kubo_api}/api/v0/version", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def probe_contract_deployment(env_values: dict[str, str]) -> bool:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    addresses = [
        env_values.get("REGISTRY_ADDRESS", "").strip(),
        env_values.get("AGGREGATOR_ADDRESS", "").strip(),
        env_values.get("GM_STORAGE_ADDRESS", "").strip(),
    ]
    valid_addresses = [address for address in addresses if re.fullmatch(r"0x[a-fA-F0-9]{40}", address)]
    if not valid_addresses:
        return False

    for index, address in enumerate(valid_addresses, start=1):
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [address, "latest"],
            "id": index,
        }
        response = _post_json(rpc_url, payload)
        code = (response or {}).get("result", "0x")
        if isinstance(code, str) and code not in {"0x", "0X", ""}:
            return True
    return False


def runtime_contract_env_values() -> dict[str, str]:
    values = read_env_values()
    if not phala_runtime_mode():
        return values
    rpc_url = os.environ.get("DYNAMIC_WORKER_RPC_URL", "").strip()
    kubo_api = os.environ.get("DYNAMIC_WORKER_KUBO_API_URL", "").strip().rstrip("/")
    if rpc_url:
        values["RPC_URL"] = rpc_url
    if kubo_api:
        values["KUBO_API"] = kubo_api
        try:
            manifest_url = kubo_api + "/api/v0/files/read?arg=" + urllib.parse.quote(
                "/runtime/contracts.json", safe=""
            )
            request = urllib.request.Request(manifest_url, data=b"", method="POST")
            with urllib.request.urlopen(request, timeout=2.0) as response:
                manifest = json.loads(response.read().decode("utf-8"))
            address_keys = {
                "registry_address": "REGISTRY_ADDRESS",
                "aggregator_address": "AGGREGATOR_ADDRESS",
                "gm_storage_address": "GM_STORAGE_ADDRESS",
            }
            for manifest_key, env_key in address_keys.items():
                address = str(manifest.get(manifest_key, "")).strip()
                if re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
                    values[env_key] = address
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass
    return values


def read_chain_round(env_values: dict[str, str]) -> int | None:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    gm_storage_address = env_values.get("GM_STORAGE_ADDRESS", "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", gm_storage_address):
        return None

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": gm_storage_address, "data": "0x9f8743f7"}, "latest"],
        "id": 1,
    }
    response = _post_json(rpc_url, payload)
    result = (response or {}).get("result")
    if not isinstance(result, str) or result in {"", "0x"}:
        return None
    try:
        return int(result, 16)
    except ValueError:
        return None


def _decode_abi_address(hex_data: str) -> str | None:
    if not isinstance(hex_data, str):
        return None
    data = hex_data.removeprefix("0x")
    if len(data) < 64:
        return None
    try:
        return "0x" + bytes.fromhex(data)[12:32].hex()
    except ValueError:
        return None


def _worker_label_for_address(env_values: dict[str, str], address: str | None) -> str | None:
    if not address:
        return None
    normalized = address.lower()
    for index in range(20):
        worker_address = env_values.get(f"W{index}_ACCOUNT_ADDRESS", "").strip().lower()
        if worker_address == normalized:
            return f"VM-{index}"
    try:
        inventory = json.loads(os.environ.get("DYNAMIC_WORKER_INVENTORY", "[]"))
    except json.JSONDecodeError:
        inventory = []
    if isinstance(inventory, dict):
        records = list(inventory.values())
    elif isinstance(inventory, list):
        records = inventory
    else:
        records = []
    if records:
        for record in records:
            if not isinstance(record, dict):
                continue
            if str(record.get("account_address", "")).strip().lower() == normalized:
                try:
                    return f"VM-{int(record.get('slot'))}"
                except (TypeError, ValueError):
                    return None
    return None


def read_current_aggregator(env_values: dict[str, str]) -> dict[str, str | None]:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    aggregator_address = env_values.get("AGGREGATOR_ADDRESS", "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", aggregator_address):
        return {"address": None, "vm": None}

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": aggregator_address, "data": "0xb0fa5f84"}, "latest"],
        "id": 1,
    }
    response = _post_json(rpc_url, payload)
    result = (response or {}).get("result")
    current_address = _decode_abi_address(result)
    return {
        "address": current_address,
        "vm": _worker_label_for_address(env_values, current_address),
    }


async def inspect_service(service_name: str) -> dict[str, Any]:
    ps_details = await run_subprocess(
        compose_command("ps", "-a", service_name, include_profile=False),
        cwd=WORKSPACE_ROOT,
        check=False,
    )
    docker_bin = None
    try:
        docker_bin = resolve_docker_bin()
    except RuntimeError:
        docker_bin = None

    fallback_name = STATIC_CONTAINER_NAMES.get(service_name)
    container_id = ""
    if docker_bin and fallback_name:
        fallback_result = await run_subprocess(
            [docker_bin, "ps", "-aq", "--filter", f"name=^/{fallback_name}$"],
            cwd=WORKSPACE_ROOT,
            check=False,
        )
        container_id = fallback_result["stdout"].strip()
    if not container_id:
        ps_result = await run_subprocess(
            compose_command("ps", "-a", "-q", service_name, include_profile=False),
            cwd=WORKSPACE_ROOT,
            check=False,
        )
        container_id = ps_result["stdout"].strip()
    if not docker_bin or not container_id:
        return compose_ps_state(service_name, ps_details["stdout"], container_id)

    inspect_result = await run_subprocess([docker_bin, "inspect", container_id], cwd=WORKSPACE_ROOT, check=True)
    payload = json.loads(inspect_result["stdout"])[0]
    state = payload.get("State", {})
    health = state.get("Health") or {}
    return {
        "service": service_name,
        "exists": True,
        "status": state.get("Status", "unknown"),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "health": health.get("Status"),
        "container_id": container_id,
        "container_name": payload.get("Name", "").lstrip("/"),
    }


async def wait_for_service_exit_success(service_name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None

    while asyncio.get_running_loop().time() < deadline:
        state = await inspect_service(service_name)
        last_state = state
        if not state["exists"] or state["status"] in {"created", "restarting"}:
            await asyncio.sleep(2)
            continue
        if state["running"]:
            await asyncio.sleep(2)
            continue
        if state["status"] == "exited" and state["exit_code"] == 0:
            return state
        raise RuntimeError(
            f"{service_name} finished unsuccessfully with status={state['status']} exit_code={state['exit_code']}."
        )

    raise RuntimeError(f"Timed out while waiting for {service_name} to finish. Last state: {last_state}")


async def phala_container_id(service_name: str) -> str:
    docker_bin = resolve_docker_bin()
    result = await run_subprocess(
        [docker_bin, "ps", "-aq", "--filter", f"label=com.docker.compose.service={service_name}"],
        cwd=Path("/app"),
        check=True,
    )
    container_ids = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    if len(container_ids) != 1:
        raise RuntimeError(
            f"expected exactly one Phala container for service {service_name}, found {len(container_ids)}"
        )
    return container_ids[0]


async def inspect_docker_container(container_id: str, service_name: str) -> dict[str, Any]:
    docker_bin = resolve_docker_bin()
    result = await run_subprocess([docker_bin, "inspect", container_id], cwd=Path("/app"), check=True)
    payload = json.loads(result["stdout"])[0]
    state = payload.get("State", {})
    return {
        "service": service_name,
        "exists": True,
        "status": state.get("Status", "unknown"),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "health": (state.get("Health") or {}).get("Status"),
        "container_id": container_id,
        "container_name": payload.get("Name", "").lstrip("/"),
    }


async def wait_for_docker_container_exit_success(
    container_id: str, service_name: str, timeout_seconds: int
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        last_state = await inspect_docker_container(container_id, service_name)
        if last_state["running"] or last_state["status"] in {"created", "restarting"}:
            await asyncio.sleep(2)
            continue
        if last_state["status"] == "exited" and last_state["exit_code"] == 0:
            return last_state
        raise RuntimeError(f"{service_name} exited unsuccessfully: {last_state}")
    raise RuntimeError(f"Timed out waiting for {service_name} to exit successfully. Last state: {last_state}")


async def wait_for_service_ready(service_name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None

    while asyncio.get_running_loop().time() < deadline:
        state = await inspect_service(service_name)
        last_state = state
        if not state["exists"] or state["status"] in {"created", "restarting"}:
            await asyncio.sleep(2)
            continue
        if not state["running"]:
            raise RuntimeError(
                f"{service_name} is not running. status={state['status']} exit_code={state['exit_code']}."
            )
        if state["health"] in {None, "healthy"}:
            return state
        if state["health"] == "unhealthy":
            raise RuntimeError(f"{service_name} became unhealthy.")
        await asyncio.sleep(2)

    raise RuntimeError(f"Timed out while waiting for {service_name} to become ready. Last state: {last_state}")


async def collect_runtime_status() -> dict[str, Any]:
    env_values = read_env_values()
    worker_services = _available_worker_services()
    static_services = ["smart-contracts", "anvil", "ipfs", "agent", "zk-inference"]
    service_state_map, json_available = await compose_ps_state_map([*static_services, *worker_services])
    if json_available:
        contract_state = service_state_map.get("smart-contracts") or missing_service_state("smart-contracts")
        anvil_state = service_state_map.get("anvil") or missing_service_state("anvil")
        ipfs_state = service_state_map.get("ipfs") or missing_service_state("ipfs")
        agent_state = service_state_map.get("agent") or missing_service_state("agent")
        zk_inference_state = service_state_map.get("zk-inference") or missing_service_state("zk-inference")
        worker_states = [
            service_state_map.get(service) or missing_service_state(service) for service in worker_services
        ]
    else:
        contract_state = await inspect_service("smart-contracts")
        anvil_state = await inspect_service("anvil")
        ipfs_state = await inspect_service("ipfs")
        agent_state = await inspect_service("agent")
        zk_inference_state = await inspect_service("zk-inference")
        worker_states = await inspect_worker_services(worker_services)
    running_workers = [state["service"] for state in worker_states if state["running"]]

    anvil_ready = probe_anvil_ready(env_values)
    ipfs_ready = probe_ipfs_ready(env_values)
    chain_contracts_ready = probe_contract_deployment(env_values) if anvil_ready else False
    current_round = read_chain_round(env_values) if chain_contracts_ready else None
    current_aggregator = read_current_aggregator(env_values) if chain_contracts_ready else {"address": None, "vm": None}

    contract_completed_successfully = contract_state["status"] == "exited" and contract_state["exit_code"] == 0
    contract_failed = contract_state["status"] == "exited" and contract_state["exit_code"] not in {None, 0}
    contract_initialized = contract_completed_successfully
    contract_running = contract_state["running"]
    training_started = contract_initialized and bool(
        running_workers or agent_state["running"] or zk_inference_state["running"]
    )

    return {
        "contract_initialized": contract_initialized,
        "contract_running": contract_running,
        "training_started": training_started,
        "agent_running": agent_state["running"],
        "base_runtime_ready": (anvil_state["running"] or anvil_ready) and (ipfs_state["running"] or ipfs_ready),
        "chain_contracts_ready": chain_contracts_ready,
        "contract_completed_successfully": contract_completed_successfully,
        "contract_failed": contract_failed,
        "current_round": current_round,
        "current_aggregator_address": current_aggregator["address"],
        "current_aggregator_vm": current_aggregator["vm"],
        "running_workers": running_workers,
        "services": {
            "anvil": anvil_state,
            "ipfs": ipfs_state,
            "smart-contracts": contract_state,
            "zk-inference": zk_inference_state,
            "agent": agent_state,
            "workers": worker_states,
        },
    }


async def reset_services(service_names: list[str]) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    if not service_names:
        return logs

    for args in (["stop", *service_names], ["rm", "-f", *service_names]):
        logs.append(await run_subprocess(compose_command(*args), cwd=WORKSPACE_ROOT, check=False))
    return logs


async def reset_observability_volumes() -> list[dict[str, Any]]:
    try:
        docker_bin = resolve_docker_bin()
    except RuntimeError as exc:
        return [
            {
                "command": ["docker", "volume", "rm", "-f", *OBSERVABILITY_VOLUME_NAMES],
                "returncode": 0,
                "stdout": "Skipped old observability volume cleanup because the Docker CLI is unavailable.",
                "stderr": str(exc),
            }
        ]
    project_name = compose_project_name()
    logs: list[dict[str, Any]] = []

    for volume_name in OBSERVABILITY_VOLUME_NAMES:
        logs.append(
            await run_subprocess(
                [docker_bin, "volume", "rm", "-f", f"{project_name}_{volume_name}"],
                cwd=WORKSPACE_ROOT,
                check=False,
            )
        )
    return logs


async def clear_evaluation_artifacts() -> list[dict[str, Any]]:
    removed: list[str] = []
    errors: list[str] = []
    evaluation_dir = WORKSPACE_ROOT / "data" / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    for pattern in EVALUATION_ARTIFACT_PATTERNS:
        for path in evaluation_dir.glob(pattern):
            try:
                path.unlink()
                removed.append(str(path.relative_to(WORKSPACE_ROOT)))
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    return [
        {
            "command": ["clear-evaluation-artifacts"],
            "returncode": 1 if errors else 0,
            "stdout": "\n".join(removed),
            "stderr": "\n".join(errors),
        }
    ]


async def wait_for_http_url(url: str, timeout_seconds: int) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=2).read())
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            await asyncio.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}. Last error: {last_error}")


async def reset_observability_state() -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    logs.extend(await reset_services(OBSERVABILITY_SERVICES))
    logs.extend(await reset_observability_volumes())
    logs.extend(await clear_evaluation_artifacts())
    logs.append(
        await run_subprocess(
            compose_command("up", "-d", *OBSERVABILITY_SERVICES),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    await wait_for_http_url("http://grafana:3000/api/health", 120)
    return logs


async def ensure_observability_services() -> list[dict[str, Any]]:
    logs = [
        await run_subprocess(
            compose_command("up", "-d", *OBSERVABILITY_SERVICES),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    ]
    await wait_for_http_url("http://grafana:3000/api/health", 120)
    return logs


async def reset_grafana_view_values() -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    logs.extend(await clear_evaluation_artifacts())
    logs.extend(await reset_services(["prometheus"]))
    logs.append(
        await run_subprocess(
            compose_command("up", "-d", "prometheus"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    await wait_for_http_url("http://prometheus:9090/-/ready", 120)
    return logs


async def initialize_contract_stack() -> dict[str, Any]:
    if phala_runtime_mode():
        worker_status = await asyncio.to_thread(phala_worker_controller().scale, 0)
        reset_runtime_telemetry()
        await clear_evaluation_artifacts()

        rpc_url = os.environ.get("DYNAMIC_WORKER_RPC_URL", "http://anvil:8545").strip()
        reset_response = await asyncio.to_thread(
            _post_json,
            rpc_url,
            {"jsonrpc": "2.0", "method": "anvil_reset", "params": [], "id": 1},
            10.0,
        )
        if reset_response is None or reset_response.get("error") is not None:
            raise RuntimeError(f"Anvil reset failed: {reset_response}")

        container_id = await phala_container_id("smart-contracts")
        docker_bin = resolve_docker_bin()
        start_result = await run_subprocess(
            [docker_bin, "start", container_id], cwd=Path("/app"), check=True
        )
        contract_state = await wait_for_docker_container_exit_success(
            container_id, "smart-contracts", CONTRACT_TIMEOUT_SECONDS
        )
        return {
            "contract_state": contract_state,
            "status": await phala_runtime_status(),
            "phala_workers": worker_status,
            "logs": [start_result],
        }

    worker_services = _available_worker_services()
    reset_targets = ["agent", "zk-inference", *worker_services, "smart-contracts", "anvil", "ipfs"]
    logs = await reset_services(reset_targets)
    logs.append(
        await run_subprocess(
            compose_command("up", "--build", "-d", "anvil", "ipfs"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    await wait_for_service_ready("anvil", 120)
    await wait_for_service_ready("ipfs", 120)
    logs.append(
        await run_subprocess(
            compose_command("up", "--build", "-d", "smart-contracts"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    contract_state = await wait_for_service_exit_success("smart-contracts", CONTRACT_TIMEOUT_SECONDS)
    return {
        "contract_state": contract_state,
        "status": await collect_runtime_status(),
        "logs": logs[-3:],
    }


async def start_training_services(config: dict[str, int]) -> dict[str, Any]:
    available_workers = _available_worker_services()
    if not available_workers:
        raise RuntimeError("No active VM-* worker services are defined in compose.yml.")

    runtime_status = await collect_runtime_status()
    if not runtime_status["contract_initialized"]:
        raise RuntimeError("Smart contracts are not initialized yet. Run contract initialization first.")

    selected_workers = available_workers[: config["worker_count"]]
    inactive_workers = available_workers[config["worker_count"] :]

    logs = await reset_services(["agent", "zk-inference", *available_workers])
    logs.extend(await reset_observability_state())
    base_services = [
        "anvil",
        "ipfs",
    ]
    if use_local_ollama():
        base_services.append("ollama")

    logs.append(
        await run_subprocess(
            compose_command("up", "-d", *base_services),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    if use_local_ollama():
        logs.append(
            await run_subprocess(
                compose_command(
                    "up",
                    "-d",
                    "ollama-init",
                ),
                cwd=WORKSPACE_ROOT,
                check=True,
            )
        )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "--no-deps",
                "-d",
                "zk-inference",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "--build",
                "--no-deps",
                "-d",
                *selected_workers,
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "--build",
                "--no-deps",
                "-d",
                "agent",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    if inactive_workers:
        logs.extend(await reset_services(inactive_workers))

    return {
        "selected_workers": selected_workers,
        "inactive_workers": inactive_workers,
        "status": await collect_runtime_status(),
        "logs": logs[-4:],
    }


async def reset_training_services() -> dict[str, Any]:
    worker_services = _available_worker_services()
    reset_targets = [
        "agent",
        "zk-inference",
        *worker_services,
        "smart-contracts",
        "anvil",
        "ipfs",
        *OBSERVABILITY_SERVICES,
        "ollama-init",
        "ollama",
    ]
    logs = await reset_services(reset_targets)
    logs.extend(await reset_observability_volumes())
    logs.extend(await clear_evaluation_artifacts())
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "-d",
                *OBSERVABILITY_SERVICES,
                "anvil",
                "ipfs",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    await wait_for_http_url("http://grafana:3000/api/health", 120)
    await wait_for_service_ready("anvil", 120)
    await wait_for_service_ready("ipfs", 120)
    logs.append(
        await run_subprocess(
            compose_command("up", "--build", "-d", "smart-contracts"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    contract_state = await wait_for_service_exit_success("smart-contracts", CONTRACT_TIMEOUT_SECONDS)
    return {
        "contract_state": contract_state,
        "status": await collect_runtime_status(),
        "logs": logs[-4:],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/telemetry/events", status_code=202)
async def ingest_telemetry(body: dict[str, Any]) -> dict[str, bool]:
    payload = body.get("payload")
    signature = str(body.get("signature", ""))
    if not isinstance(payload, dict) or not re.fullmatch(r"0x[a-fA-F0-9]{130}", signature):
        raise HTTPException(status_code=400, detail="invalid signed telemetry envelope")
    try:
        _record_telemetry(payload, signature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"accepted": True}


@app.get("/metrics")
async def metrics() -> Response:
    env_values = runtime_contract_env_values()
    current_round = read_chain_round(env_values)
    current_aggregator = read_current_aggregator(env_values)
    telemetry_records = _telemetry_snapshot()
    evaluation_records = read_evaluation_summary_records() + telemetry_evaluation_records(telemetry_records)
    transaction_cost_records = read_transaction_cost_records() + telemetry_transaction_cost_records(telemetry_records)
    round_value = current_round if current_round is not None else 0
    aggregator_vm = current_aggregator["vm"] or "unknown"
    payload_lines = [
        "# HELP dfl_current_round Current DFL round read directly from the GMStorage smart contract.",
        "# TYPE dfl_current_round gauge",
        f"dfl_current_round {round_value}",
        "",
        "# HELP dfl_current_aggregator Current DFL aggregator selected on-chain.",
        "# TYPE dfl_current_aggregator gauge",
        f'dfl_current_aggregator{{aggregator_vm="{aggregator_vm}"}} 1',
        "",
    ]
    runtime_totals = telemetry_run_totals(telemetry_records) if phala_runtime_mode() else None
    append_evaluation_metrics(payload_lines, evaluation_records, runtime_totals)
    append_transaction_cost_metrics(payload_lines, transaction_cost_records)
    payload_lines.extend(
        [
            "",
            "# HELP dfl_worker_runtime_event_total Signed runtime events received from each Phala worker.",
            "# TYPE dfl_worker_runtime_event_total gauge",
        ]
    )
    event_totals: dict[tuple[str, str, str], int] = {}
    for record in telemetry_records:
        key = (
            str(record.get("device_id", "")),
            str(record.get("account", "")),
            str(record.get("event", "")),
        )
        event_totals[key] = event_totals.get(key, 0) + 1
    for (device_id, account, event), value in event_totals.items():
        worker = _worker_display_name(device_id, account)
        labels = (
            f'worker="{_prometheus_label_value(worker)}",'
            f'account="{_prometheus_label_value(account)}",'
            f'event="{_prometheus_label_value(event)}"'
        )
        payload_lines.append(f"dfl_worker_runtime_event_total{{{labels}}} {value}")
    payload = "\n".join(payload_lines)
    return Response(content=payload, media_type="text/plain; version=0.0.4")


async def phala_runtime_status() -> dict[str, Any]:
    worker_status = await asyncio.to_thread(phala_worker_controller().status)
    deployed = worker_status["deployed_worker_count"]
    contract_initialized = environment_flag("PHALA_CONTRACTS_READY", default=True)
    agent_available = environment_flag("PHALA_AGENT_AVAILABLE")
    return {
        "contract_initialized": contract_initialized,
        "contract_running": False,
        "training_started": contract_initialized and deployed > 0,
        "agent_running": agent_available,
        "base_runtime_ready": True,
        "chain_contracts_ready": contract_initialized,
        "contract_completed_successfully": contract_initialized,
        "contract_failed": False,
        "current_round": None,
        "current_aggregator_address": None,
        "current_aggregator_vm": None,
        "running_workers": [worker["worker"] for worker in worker_status["workers"]],
        "services": {
            "anvil": {"service": "anvil", "status": "running", "running": True},
            "ipfs": {"service": "ipfs", "status": "running", "running": True},
            "smart-contracts": {
                "service": "smart-contracts",
                "status": "exited" if contract_initialized else "starting",
                "running": False,
                "exit_code": 0 if contract_initialized else None,
            },
            "agent": {
                "service": "agent",
                "status": "external" if agent_available else "not-deployed",
                "running": agent_available,
            },
            "zk-inference": {"service": "zk-inference", "status": "external", "running": True},
            "workers": worker_status["workers"],
        },
        "phala_workers": worker_status,
    }


@app.get("/api/phala/workers")
async def get_phala_workers(request: Request) -> dict[str, Any]:
    require_control_admin(request)
    try:
        return await asyncio.to_thread(phala_worker_controller().status)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/phala/workers/scale")
async def scale_phala_workers(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    require_control_admin(request)
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")
    try:
        worker_count = int(payload.get("worker_count"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="worker_count must be an integer") from exc
    async with operation_lock:
        try:
            return await asyncio.to_thread(phala_worker_controller().scale, worker_count)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/phala/workers")
async def destroy_phala_workers(request: Request) -> dict[str, Any]:
    require_control_admin(request)
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")
    async with operation_lock:
        try:
            return await asyncio.to_thread(phala_worker_controller().scale, 0)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/control/status")
async def get_control_status() -> dict[str, Any]:
    try:
        if phala_runtime_mode():
            return {
                "config": read_training_config(),
                "runtime": await phala_runtime_status(),
            }
        return {
            "config": read_training_config(),
            "runtime": await collect_runtime_status(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/observability/ensure")
async def ensure_observability() -> dict[str, Any]:
    if phala_runtime_mode():
        if not environment_flag("PHALA_OBSERVABILITY_AVAILABLE"):
            raise HTTPException(
                status_code=503,
                detail="Observability services are not deployed in the current Phala runtime.",
            )
        return {"ok": True, "managed_by": "phala-compose", "logs": []}
    try:
        logs = await ensure_observability_services()
        return {"ok": True, "logs": logs[-1:]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/observability/reset-values")
async def reset_observability_values() -> dict[str, Any]:
    if phala_runtime_mode() and not environment_flag("PHALA_OBSERVABILITY_AVAILABLE"):
        raise HTTPException(
            status_code=503,
            detail="Observability services are not deployed in the current Phala runtime.",
        )
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            if phala_runtime_mode():
                reset_runtime_telemetry()
                return {"ok": True, "status": await phala_runtime_status(), "logs": []}
            logs = await reset_grafana_view_values()
            return {"ok": True, "status": await collect_runtime_status(), "logs": logs[-3:]}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/control/contracts/initialize")
async def initialize_contracts(request: Request) -> dict[str, Any]:
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            if phala_runtime_mode():
                require_control_admin(request)
            return {"ok": True, **await initialize_contract_stack()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/training/config")
async def get_training_config() -> dict[str, Any]:
    try:
        return read_training_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/training/config")
async def update_training_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = normalize_training_config(payload)
        config = write_training_config(normalized)
        return {"ok": True, "config": config}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/training/start")
async def start_training(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            normalized = normalize_training_config(payload)
            if phala_runtime_mode():
                require_control_admin(request)
                worker_config = phala_worker_training_config(normalized)
                worker_status = await asyncio.to_thread(
                    phala_worker_controller().scale,
                    normalized["worker_count"],
                    worker_config,
                )
                write_training_config(normalized)
                reset_runtime_telemetry()
                return {
                    "ok": True,
                    "config": {**read_training_config(), **normalized},
                    "selected_workers": [worker["worker"] for worker in worker_status["workers"]],
                    "inactive_workers": [],
                    "status": await phala_runtime_status(),
                    "phala_workers": worker_status,
                    "logs": [],
                }
            saved_config = write_training_config(normalized)
            result = await start_training_services(normalized)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "config": saved_config, **result}


@app.post("/api/training/reset")
async def reset_training(request: Request) -> dict[str, Any]:
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            if phala_runtime_mode():
                require_control_admin(request)
                result = await initialize_contract_stack()
                return {
                    "ok": True,
                    "config": read_training_config(),
                    **result,
                }
            result = await reset_training_services()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "config": read_training_config(), **result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CONTROL_API_PORT", "8091")), log_level="warning")
