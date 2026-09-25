"""Explicit cooperative upload fault injection; disabled outside evaluation deployments.

Decisions are read-only. Only administrator operations and independently authenticated
receiver telemetry can change the persisted gate or its bounded audit counters.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any


class GateError(ValueError):
    pass


def enabled() -> bool:
    return os.environ.get("EVALUATION_GATES_ENABLED", "false").lower() in {"1", "true"}


def run_identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", value):
        raise GateError("run_id must be a fresh 32-128 character identifier")
    return value


class UploadGate:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def _read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1:
            raise GateError("invalid persisted upload gate")
        return value

    def _write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def arm(self, run_id: str, round: int, allowed_clients: int, worker_count: int) -> dict[str, Any]:
        run_identifier(run_id)
        if type(round) is not int or round != 1:
            raise GateError("evaluation gate only supports training round 1")
        if type(allowed_clients) is not int or not 0 <= allowed_clients < worker_count:
            raise GateError("allowed_clients must be between zero and worker_count minus one")
        with self.lock:
            current = self._read()
            if current is not None:
                if (current["run_id"], current["round"], current["allowed_clients"]) == (
                    run_id,
                    round,
                    allowed_clients,
                ) and current["active"]:
                    return dict(current)
                raise GateError("reset the previous gate before arming another run")
            record = {
                "version": 1,
                "run_id": run_id,
                "round": round,
                "allowed_clients": allowed_clients,
                "worker_count": worker_count,
                "active": True,
                "armed": True,
                "released": False,
                "roster": [],
                "blocked_counts": {},
                "blocked_total": 0,
                "observed_aggregator": None,
            }
            self._write(record)
            return record

    def bind_roster(self, roster: list[str]) -> str:
        with self.lock:
            record = self._read()
            if record is None:
                return ""
            if not record["active"]:
                raise GateError("a released gate cannot be reused for a new training run")
            normalized = [str(x).lower() for x in roster]
            if (
                len(normalized) != record["worker_count"]
                or len(set(normalized)) != len(normalized)
                or any(not re.fullmatch(r"0x[0-9a-f]{40}", x) or int(x[2:], 16) == 0 for x in normalized)
            ):
                raise GateError("gate roster must match the committed worker set")
            if record["roster"] and record["roster"] != normalized:
                raise GateError("gate roster is immutable")
            record["roster"] = normalized
            self._write(record)
            return record["run_id"]

    @staticmethod
    def _selection(record: dict[str, Any], aggregator: str) -> list[str]:
        if aggregator not in record["roster"]:
            raise GateError("aggregator is outside the committed gate roster")
        return [account for account in record["roster"] if account != aggregator][: record["allowed_clients"]]

    def decision(
        self, run_id: str, round: int, aggregator: str, participant: str, *, chain_round: int, chain_aggregator: str
    ) -> dict[str, Any]:
        with self.lock:
            record = self._read()
            if record is None or record["run_id"] != run_identifier(run_id):
                raise GateError("unknown evaluation gate generation")
            if type(round) is not int or round < 1 or round != chain_round or aggregator != chain_aggregator:
                raise GateError("gate request does not match the current chain round and aggregator")
            if participant not in record["roster"] or participant == aggregator:
                raise GateError("gate request is not a training participant")
            selected = self._selection(record, aggregator)
            applies = record["active"] and round == record["round"]
            return {
                "run_id": run_id,
                "round": round,
                "gate_round": record["round"],
                "aggregator": aggregator,
                "participant": participant,
                "active": record["active"],
                "applies": applies,
                "allow": not applies or participant in selected,
            }

    def record_ready(
        self, receiver: str, attributes: dict[str, Any], *, chain_round: int, chain_aggregator: str
    ) -> None:
        with self.lock:
            record = self._read()
            if record is None or record["run_id"] != run_identifier(attributes.get("run_id")):
                raise GateError("unknown evaluation gate generation")
            if (
                not record["active"]
                or attributes.get("round") != record["round"]
                or chain_round != record["round"]
                or receiver != chain_aggregator
            ):
                raise GateError("receiver readiness does not match the active gate round")
            self._selection(record, receiver)
            if record["observed_aggregator"] not in (None, receiver):
                raise GateError("gate round aggregator is immutable")
            record["observed_aggregator"] = receiver
            self._write(record)

    def record_block(
        self, receiver: str, attributes: dict[str, Any], *, chain_round: int, chain_aggregator: str
    ) -> None:
        with self.lock:
            decision = self.decision(
                attributes.get("run_id"),
                attributes.get("round"),
                receiver,
                str(attributes.get("participant", "")).lower(),
                chain_round=chain_round,
                chain_aggregator=chain_aggregator,
            )
            if decision["allow"]:
                raise GateError("telemetry does not describe a blocked gate participant")
            record = self._read()
            participant = decision["participant"]
            if record["observed_aggregator"] not in (None, receiver):
                raise GateError("gate round aggregator is immutable")
            record["observed_aggregator"] = receiver
            record["blocked_counts"][participant] = min(record["blocked_counts"].get(participant, 0) + 1, 2**31 - 1)
            record["blocked_total"] = min(record["blocked_total"] + 1, 2**31 - 1)
            self._write(record)

    def status(self, *, chain_round: int | None = None, chain_aggregator: str | None = None) -> dict[str, Any]:
        with self.lock:
            record = self._read()
            if record is None:
                return {
                    "active": False,
                    "armed": False,
                    "released": True,
                    "run_id": None,
                    "blocked_participants": [],
                    "blocked_counts": {},
                    "blocked_total": 0,
                }
            result = dict(record)
            result["blocked_participants"] = sorted(record["blocked_counts"])
            aggregator = record["observed_aggregator"] or (chain_aggregator if chain_round == record["round"] else None)
            result["selected_aggregator"] = aggregator
            result["allowed_participants"] = (
                self._selection(record, aggregator) if aggregator and record["roster"] else []
            )
            result["selection_ready"] = bool(aggregator and record["roster"])
            result["cooperative_fault_injection"] = True
            return result

    def release(self, run_id: str) -> dict[str, Any]:
        run_identifier(run_id)
        with self.lock:
            record = self._read()
            if record is None:
                # An arm whose request never arrived still needs safe cleanup.
                return {"run_id": run_id, "released": True, "active": False}
            if record["run_id"] != run_id:
                raise GateError("cannot release another evaluation gate generation")
            record.update(active=False, released=True)
            self._write(record)
            return dict(record)

    def reset(self) -> None:
        with self.lock:
            current = self._read()
            if current is not None and current["active"]:
                raise GateError("release the active evaluation gate before resetting training")
            self.path.unlink(missing_ok=True)
