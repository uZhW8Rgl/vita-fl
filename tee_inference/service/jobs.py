"""Ephemeral ChestMNIST inference jobs owned by the TEE service."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tee_inference.protocol.v1 import LABELS, encode_deterministic


class JobError(RuntimeError):
    """A requested inference job cannot be created or executed."""


@dataclass(frozen=True)
class TeeInferenceJob:
    job_id: str
    source_index: int
    pixels: bytes
    ground_truth: bytes
    manifest_hash: bytes
    created_at_ms: int

    def request_bytes(self) -> bytes:
        return encode_deterministic(
            {
                1: 1,
                2: secrets.token_bytes(16),
                3: self.manifest_hash,
                4: self.pixels,
                5: secrets.token_bytes(32),
                6: int(time.time() * 1000),
            }
        )

    def public_metadata(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "source_index": self.source_index,
            "ground_truth": [LABELS[position] for position, value in enumerate(self.ground_truth) if value],
            "manifest_sha256": self.manifest_hash.hex(),
            "created_at_ms": self.created_at_ms,
        }


class TeeJobStore:
    def __init__(self, root: Path, dataset_path: Path) -> None:
        self.root = root
        self.dataset_path = dataset_path
        self._jobs: dict[str, TeeInferenceJob] = {}
        self._lock = threading.RLock()

    def create(self, index: int | None, manifest_hash: bytes) -> TeeInferenceJob:
        if len(manifest_hash) != 32:
            raise JobError("a prepared 32-byte model manifest hash is required")
        if not self.dataset_path.is_file():
            raise JobError(f"ChestMNIST test dataset is missing: {self.dataset_path}")
        with np.load(self.dataset_path, allow_pickle=False) as dataset:
            images = dataset["images"]
            labels = dataset["labels"]
            if images.ndim != 3 or images.shape[1:] != (28, 28) or images.dtype != np.uint8:
                raise JobError("ChestMNIST images must be uint8 [N,28,28]")
            if labels.shape != (images.shape[0], 14):
                raise JobError("ChestMNIST labels must be [N,14]")
            selected = secrets.randbelow(images.shape[0]) if index is None else int(index)
            if not 0 <= selected < images.shape[0]:
                raise JobError(f"sample index must be in [0,{images.shape[0] - 1}]")
            pixels = images[selected].tobytes(order="C")
            ground_truth = bytes(int(value) for value in labels[selected])

        job = TeeInferenceJob(
            job_id=secrets.token_hex(16),
            source_index=selected,
            pixels=pixels,
            ground_truth=ground_truth,
            manifest_hash=manifest_hash,
            created_at_ms=int(time.time() * 1000),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            directory = self.root / job.job_id
            directory.mkdir(parents=True, exist_ok=False)
            (directory / "pixels.bin").write_bytes(job.pixels)
            (directory / "selection.json").write_text(
                json.dumps(job.public_metadata(), sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        return job

    def get(self, job_id: str) -> TeeInferenceJob:
        if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
            raise JobError("job_id must contain exactly 32 lowercase hexadecimal characters")
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise JobError("TEE inference job does not exist in this container run") from exc

    def store_evidence(self, job_id: str, evidence: bytes) -> Path:
        self.get(job_id)
        path = self.root / job_id / "evidence.cbor"
        temporary = path.with_suffix(".cbor.tmp")
        temporary.write_bytes(evidence)
        temporary.replace(path)
        return path
