"""Deterministic protocol adapter around the ChestMNIST ONNX model."""

from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

from tee_inference.protocol.v1 import (
    build_response,
    decode_manifest,
    decode_request,
    encode_deterministic,
)


class InferenceError(ValueError):
    """The model or submitted request cannot produce a valid response."""


class ChestMnistOnnxEngine:
    def __init__(self, model_path: Path, model_manifest_hash: bytes) -> None:
        if len(model_manifest_hash) != 32:
            raise ValueError("model manifest hash must contain 32 bytes")
        self.model_path = model_path.resolve()
        self.model_manifest_hash = model_manifest_hash
        self.model_artifact_hash = hashlib.sha256(self.model_path.read_bytes()).digest()
        options = ort.SessionOptions()
        options.enable_mem_pattern = False
        options.enable_cpu_mem_arena = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(self.model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._validate_model_contract()

    @classmethod
    def from_manifest(cls, model_path: Path, exact_manifest: bytes) -> "ChestMnistOnnxEngine":
        manifest = decode_manifest(exact_manifest)
        artifact = manifest[4]
        engine = cls(model_path, hashlib.sha256(exact_manifest).digest())
        if engine.model_artifact_hash != artifact[3]:
            raise InferenceError("ONNX artifact hash does not match model manifest")
        if engine.model_path.stat().st_size != artifact[4]:
            raise InferenceError("ONNX artifact size does not match model manifest")
        return engine

    def _validate_model_contract(self) -> None:
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if len(inputs) != 1 or inputs[0].name != "input" or inputs[0].type != "tensor(float)":
            raise InferenceError("ONNX model must have one float32 input named 'input'")
        if list(inputs[0].shape) != [1, 784]:
            raise InferenceError(f"unexpected ONNX input shape: {inputs[0].shape}")
        if len(outputs) != 1 or outputs[0].name != "logits" or outputs[0].type != "tensor(float)":
            raise InferenceError("ONNX model must have one float32 output named 'logits'")
        if list(outputs[0].shape) != [1, 14]:
            raise InferenceError(f"unexpected ONNX output shape: {outputs[0].shape}")

    @staticmethod
    def _input_tensor(pixels: bytes) -> np.ndarray:
        raw = np.frombuffer(pixels, dtype=np.uint8).astype(np.float64)
        return ((raw - 127.5) / 127.5).astype(np.float32).reshape(1, 784)

    def infer(self, exact_request: bytes) -> bytes:
        request = decode_request(exact_request)
        if request[3] != self.model_manifest_hash:
            raise InferenceError("request model manifest hash does not match loaded model")
        tensor = self._input_tensor(request[4])
        started = time.perf_counter_ns()
        result = self.session.run(["logits"], {"input": tensor})[0]
        duration_us = max(0, (time.perf_counter_ns() - started) // 1_000)
        logits = np.asarray(result, dtype=np.float32).reshape(-1)
        if logits.shape != (14,) or not np.isfinite(logits).all():
            raise InferenceError("ONNX output must contain 14 finite logits")
        probabilities = np.empty_like(logits)
        for index, logit in enumerate(logits):
            value = float(logit)
            probabilities[index] = (
                1.0 / (1.0 + math.exp(-value))
                if value >= 0
                else math.exp(value) / (1.0 + math.exp(value))
            )
        decisions = bytes(int(value >= np.float32(0.5)) for value in probabilities)
        response = build_response(
            request_id=request[2],
            exact_request=exact_request,
            model_manifest_hash=self.model_manifest_hash,
            logits=[float(value) for value in logits],
            probabilities=[float(value) for value in probabilities],
            decisions=decisions,
            duration_microseconds=duration_us,
        )
        return encode_deterministic(response)
