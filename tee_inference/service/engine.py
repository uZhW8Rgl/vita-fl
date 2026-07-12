"""Deterministic protocol adapter around the native DFL PyTorch model."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

os.environ["DATASET_NAME"] = "chestmnist"

import torch  # noqa: E402

from dfl.neural_network.cli import MODEL_LAYOUT, read_model_bin  # noqa: E402

from tee_inference.protocol.v1 import (
    build_response,
    decode_manifest,
    decode_request,
    encode_deterministic,
)


class InferenceError(ValueError):
    """The model or submitted request cannot produce a valid response."""


class ChestMnistTorchEngine:
    def __init__(self, model_path: Path, model_manifest_hash: bytes) -> None:
        if len(model_manifest_hash) != 32:
            raise ValueError("model manifest hash must contain 32 bytes")
        self.model_path = model_path.resolve()
        self.model_manifest_hash = model_manifest_hash
        self.model_artifact_hash = hashlib.sha256(self.model_path.read_bytes()).digest()
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        torch.use_deterministic_algorithms(True)
        self.model = read_model_bin(self.model_path).eval()
        self._validate_model_contract()

    @classmethod
    def from_manifest(cls, model_path: Path, exact_manifest: bytes) -> "ChestMnistTorchEngine":
        manifest = decode_manifest(exact_manifest)
        artifact = manifest[4]
        engine = cls(model_path, hashlib.sha256(exact_manifest).digest())
        if engine.model_artifact_hash != artifact[3]:
            raise InferenceError("native model hash does not match model manifest")
        if engine.model_path.stat().st_size != artifact[4]:
            raise InferenceError("native model size does not match model manifest")
        return engine

    def _validate_model_contract(self) -> None:
        actual = tuple((name, tuple(tensor.shape)) for name, tensor in self.model.state_dict().items())
        if actual != MODEL_LAYOUT:
            raise InferenceError("native model layout does not match FederatedCNN ChestMNIST")
        if any(tensor.dtype != torch.float64 for tensor in self.model.state_dict().values()):
            raise InferenceError("native model parameters must use float64")

    @staticmethod
    def _input_tensor(pixels: bytes) -> torch.Tensor:
        raw = torch.tensor(list(pixels), dtype=torch.float64)
        return ((raw - 127.5) / 127.5).reshape(1, 784).contiguous()

    def infer(self, exact_request: bytes) -> bytes:
        request = decode_request(exact_request)
        if request[3] != self.model_manifest_hash:
            raise InferenceError("request model manifest hash does not match loaded model")
        tensor = self._input_tensor(request[4])
        started = time.perf_counter_ns()
        with torch.inference_mode():
            logits_tensor = self.model(tensor).reshape(-1)
            probabilities_tensor = torch.sigmoid(logits_tensor)
        duration_us = max(0, (time.perf_counter_ns() - started) // 1_000)
        if logits_tensor.shape != (14,) or not torch.isfinite(logits_tensor).all():
            raise InferenceError("native model output must contain 14 finite logits")
        logits = [float(value) for value in logits_tensor]
        probabilities = [float(value) for value in probabilities_tensor]
        decisions = bytes(int(value >= 0.5) for value in probabilities)
        response = build_response(
            request_id=request[2],
            exact_request=exact_request,
            model_manifest_hash=self.model_manifest_hash,
            logits=logits,
            probabilities=probabilities,
            decisions=decisions,
            duration_microseconds=duration_us,
        )
        return encode_deterministic(response)
