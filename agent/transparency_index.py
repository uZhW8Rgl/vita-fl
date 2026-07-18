"""Local read model for SCITT submissions whose receipts were verified by the agent."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_INDEX_PATH = Path(
    os.environ.get("TRANSPARENCY_INDEX_PATH", "/tmp/transparency-log/records.jsonl")
)


def _receipt_transactions(transparency: dict[str, Any]) -> list[dict[str, str]]:
    transactions: list[dict[str, str]] = []
    for receipt in transparency.get("receipts", []):
        if not isinstance(receipt, dict):
            continue
        transactions.append(
            {
                key: str(receipt[key])
                for key in ("iss", "sigtxid", "regtxid")
                if receipt.get(key) is not None
            }
        )
    return transactions


def record_transparency_entry(
    *,
    evidence_type: str,
    job_id: str,
    model_id: str,
    transparency: dict[str, Any],
    verification: dict[str, Any],
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Append one receipt-verified SCITT entry to the UI read model."""

    path = index_path or DEFAULT_INDEX_PATH
    transaction_id = str(transparency["transaction_id"])
    record_id = hashlib.sha256(
        f"{evidence_type}:{transaction_id}:{transparency['evidence_sha256']}".encode("utf-8")
    ).hexdigest()
    record = {
        "record_id": record_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "evidence_type": evidence_type,
        "job_id": job_id,
        "model_id": model_id,
        "status": transparency["status"],
        "transaction_id": transaction_id,
        "content_type": transparency["content_type"],
        "evidence_sha256": transparency["evidence_sha256"],
        "signed_statement_sha256": transparency["signed_statement_sha256"],
        "transparent_statement_sha256": transparency["transparent_statement_sha256"],
        "transparent_statement_bytes": transparency["transparent_statement_bytes"],
        "receipt_transactions": _receipt_transactions(transparency),
        "verification": verification,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
            os.fsync(output.fileno())
    return record


def read_transparency_entries(
    *,
    limit: int = 100,
    token_ref: str | None = None,
    index_path: Path | None = None,
) -> list[dict[str, Any]]:
    path = index_path or DEFAULT_INDEX_PATH
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            if token_ref is not None and str((record.get("verification") or {}).get("token_ref", "")) != token_ref:
                continue
            records.append(record)
    return records[-max(1, min(limit, 500)) :][::-1]
