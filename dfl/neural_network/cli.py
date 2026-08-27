from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from .dicom_provenance import (
    VALIDATION_SPLIT_ID,
    validation_semantic_sha256,
    verify_training_provenance,
)
from .hybrid_r import (
    DEFAULT_MAX_LOSS_INCREASE_BPS,
    HYBRID_R_EVIDENCE_VERSION,
    HYBRID_R_V1_HASH,
    run_hybrid_r,
    write_canonical_json,
)


def _configure_torch_runtime_from_environment() -> None:
    """Apply optional per-worker CPU limits before Torch starts parallel work."""
    settings = (
        ("DFL_TORCH_NUM_THREADS", torch.set_num_threads),
        ("DFL_TORCH_INTEROP_THREADS", torch.set_num_interop_threads),
    )
    for name, setter in settings:
        raw_value = os.environ.get(name, "").strip()
        if not raw_value:
            continue
        value = int(raw_value)
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
        setter(value)


_configure_torch_runtime_from_environment()
torch.backends.nnpack.enabled = False
torch.backends.nnpack.set_flags(False)

try:
    import zmq
except ImportError:  # pragma: no cover - handled at runtime
    zmq = None

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover - handled at runtime
    hashes = serialization = padding = Cipher = algorithms = modes = None


INPUT_SIZE = 784
IMAGE_HEIGHT = 28
IMAGE_WIDTH = 28
IMAGE_CHANNELS = 1
CONV1_OUT_CHANNELS = 8
CONV2_OUT_CHANNELS = 16
CONV_KERNEL_SIZE = 3
CONV_STRIDE = 2
CONV_PADDING = 1
FC_HIDDEN_SIZE = 32
DATASET_NAME = os.environ.get("DATASET_NAME", "mnist").strip().lower()
CHESTMNIST_NUM_LABELS = 14
OUTPUT_SIZE = CHESTMNIST_NUM_LABELS if DATASET_NAME == "chestmnist" else 10
BATCH_SIZE = 32
NUM_TRAIN_IMAGES = 10_000
LEARNING_RATE = 0.1
CHESTMNIST_LEAKY_RELU_SLOPE = 0.1
CHESTMNIST_DEFAULT_LEARNING_RATE = 0.003
CHESTMNIST_DEFAULT_WEIGHT_DECAY = 0.0001
CHESTMNIST_DEFAULT_POS_WEIGHT_CAP = 10.0
CHESTMNIST_DEFAULT_GRAD_CLIP_NORM = 5.0


def model_layout() -> tuple[tuple[str, tuple[int, ...]], ...]:
    return (
        ("conv1.weight", (CONV1_OUT_CHANNELS, IMAGE_CHANNELS, CONV_KERNEL_SIZE, CONV_KERNEL_SIZE)),
        ("conv1.bias", (CONV1_OUT_CHANNELS,)),
        ("conv2.weight", (CONV2_OUT_CHANNELS, CONV1_OUT_CHANNELS, CONV_KERNEL_SIZE, CONV_KERNEL_SIZE)),
        ("conv2.bias", (CONV2_OUT_CHANNELS,)),
        ("fc1.weight", (FC_HIDDEN_SIZE, CONV2_OUT_CHANNELS * 7 * 7)),
        ("fc1.bias", (FC_HIDDEN_SIZE,)),
        ("fc2.weight", (OUTPUT_SIZE, FC_HIDDEN_SIZE)),
        ("fc2.bias", (OUTPUT_SIZE,)),
    )


MODEL_LAYOUT = model_layout()


class FederatedCNN(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(
            IMAGE_CHANNELS,
            CONV1_OUT_CHANNELS,
            kernel_size=CONV_KERNEL_SIZE,
            stride=CONV_STRIDE,
            padding=CONV_PADDING,
        )
        self.conv2 = nn.Conv2d(
            CONV1_OUT_CHANNELS,
            CONV2_OUT_CHANNELS,
            kernel_size=CONV_KERNEL_SIZE,
            stride=CONV_STRIDE,
            padding=CONV_PADDING,
        )
        self.fc1 = nn.Linear(CONV2_OUT_CHANNELS * 7 * 7, FC_HIDDEN_SIZE)
        self.fc2 = nn.Linear(FC_HIDDEN_SIZE, OUTPUT_SIZE)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.reshape(-1, IMAGE_CHANNELS, IMAGE_HEIGHT, IMAGE_WIDTH)
        x = self._hidden_activation(self.conv1(x))
        x = self._hidden_activation(self.conv2(x))
        x = x.reshape(x.shape[0], -1)
        x = self._hidden_activation(self.fc1(x))
        return self.fc2(x)

    @staticmethod
    def _hidden_activation(x: torch.Tensor) -> torch.Tensor:
        if DATASET_NAME == "chestmnist":
            return F.leaky_relu(x, negative_slope=CHESTMNIST_LEAKY_RELU_SLOPE)
        return torch.tanh(x)


FederatedMLP = FederatedCNN


def ensure_crypto() -> None:
    if serialization is None or Cipher is None:
        raise RuntimeError("cryptography is required. Install it with: pip install cryptography")


def ensure_zmq() -> None:
    if zmq is None:
        raise RuntimeError("pyzmq is required. Install it with: pip install pyzmq")


def node_server_dir() -> Path:
    override = os.environ.get("THESIS_NODE_SERVER_DIR")
    if override:
        return Path(override).resolve()
    return Path.cwd()


def repo_root() -> Path:
    candidates = [
        node_server_dir().parent,
        *Path(__file__).resolve().parents,
    ]
    for candidate in candidates:
        if (candidate / "data" / "mnist").exists() or (candidate / "data" / "chestmnist").exists():
            return candidate
    return node_server_dir().parent


def data_dir() -> Path:
    return node_server_dir() / "data"


def results_dir() -> Path:
    return data_dir() / "results_iid"


def evaluation_dir() -> Path:
    return data_dir() / "evaluation"


def received_models_dir() -> Path:
    return node_server_dir() / "received_models"


def aggregation_inputs_dir() -> Path:
    return results_dir() / "aggregation_inputs"


def validation_dataset_path() -> Path:
    return data_dir() / "validation-data.npz"


def private_key_path(path: str | None = None) -> Path:
    return node_server_dir() / (path or "private_key.pem")


def input_data_file(name: str) -> Path:
    candidates = [
        data_dir() / name,
        repo_root() / "data" / "mnist" / "data" / name,
        repo_root() / "data" / "chestmnist" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def model_byte_size() -> int:
    return sum(shape_numel(shape) * 8 for _, shape in MODEL_LAYOUT)


def read_idx_image_count(path: Path) -> int:
    blob = path.read_bytes()
    if len(blob) < 16:
        raise ValueError(f"{path} is too small to be a valid IDX image file")
    return (len(blob) - 16) // INPUT_SIZE


def read_idx_label_count(path: Path) -> int:
    blob = path.read_bytes()
    if len(blob) < 8:
        raise ValueError(f"{path} is too small to be a valid IDX label file")
    return len(blob) - 8


def shape_numel(shape: tuple[int, ...]) -> int:
    count = 1
    for dim in shape:
        count *= dim
    return count


def _read_tensor(blob: bytes, offset: int, shape: tuple[int, ...]) -> tuple[torch.Tensor, int]:
    count = shape_numel(shape)
    byte_count = count * 8
    chunk = blob[offset : offset + byte_count]
    if len(chunk) != byte_count:
        raise ValueError("Unexpected end of model file")
    values = torch.tensor(struct.unpack("<" + "d" * count, chunk), dtype=torch.float64)
    if not torch.isfinite(values).all():
        raise ValueError("Model file contains a non-finite parameter")
    return values.reshape(shape).contiguous(), offset + byte_count


def model_from_bytes(blob: bytes, source: str = "<memory>") -> FederatedCNN:
    model = FederatedCNN().double()
    offset = 0
    state = {}
    for name, shape in MODEL_LAYOUT:
        tensor, offset = _read_tensor(blob, offset, shape)
        state[name] = tensor
    if offset != len(blob):
        raise ValueError(f"{source} contains {len(blob) - offset} trailing bytes")
    model.load_state_dict(state)
    model.train()
    return model


def read_model_bin(path: Path) -> FederatedCNN:
    return model_from_bytes(path.read_bytes(), str(path))


def tensor_to_save_bytes(tensor: torch.Tensor, shape: tuple[int, ...]) -> bytes:
    values = tensor.detach().cpu().to(torch.float64)
    if tuple(values.shape) != shape:
        raise ValueError(f"Tensor shape mismatch: got {tuple(values.shape)}, expected {shape}")
    if not torch.isfinite(values).all():
        raise ValueError("Refusing to serialize a model with a non-finite parameter")
    flat = values.reshape(-1).tolist()
    return struct.pack("<" + "d" * len(flat), *flat)


def model_to_bytes(model: FederatedCNN) -> bytes:
    state = model.state_dict()
    return b"".join(tensor_to_save_bytes(state[name], shape) for name, shape in MODEL_LAYOUT)


def write_model_bin(model: FederatedCNN, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(model_to_bytes(model))


def random_model(seed: int | None = None) -> FederatedCNN:
    """Create a reproducible bootstrap with PyTorch's fan-in-aware defaults."""
    resolved_seed = int(os.environ.get("DFL_MODEL_SEED", "42")) if seed is None else int(seed)
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(resolved_seed)
        return FederatedCNN().double()


def read_idx_labels(path: Path, limit: int = NUM_TRAIN_IMAGES) -> torch.Tensor:
    blob = path.read_bytes()
    labels = torch.tensor(list(blob[8 : 8 + limit]), dtype=torch.long)
    if labels.numel() < limit:
        raise ValueError(f"{path} contains only {labels.numel()} labels")
    return labels


def read_idx_images(path: Path, limit: int = NUM_TRAIN_IMAGES) -> torch.Tensor:
    blob = path.read_bytes()
    raw = torch.tensor(list(blob[16 : 16 + limit * INPUT_SIZE]), dtype=torch.float64)
    if raw.numel() < limit * INPUT_SIZE:
        raise ValueError(f"{path} contains too few image bytes")
    return ((raw.reshape(limit, INPUT_SIZE) - 127.5) / 127.5).contiguous()


def read_npz_images_and_labels(path: Path) -> tuple[torch.Tensor, torch.Tensor]:
    with np.load(path) as bundle:
        if "images" not in bundle or "labels" not in bundle:
            raise ValueError(f"{path} must contain 'images' and 'labels' arrays")
        images = bundle["images"]
        labels = bundle["labels"]

    if images.ndim == 4 and images.shape[-1] == 1:
        images = images[..., 0]
    if images.ndim != 3 or tuple(images.shape[1:]) != (IMAGE_HEIGHT, IMAGE_WIDTH):
        raise ValueError(f"{path} has unexpected image shape {images.shape}; expected (N, 28, 28)")

    flat_images = torch.from_numpy(images.reshape(images.shape[0], INPUT_SIZE)).to(torch.float64)
    normalized_images = ((flat_images - 127.5) / 127.5).contiguous()
    label_tensor = torch.from_numpy(labels)
    return normalized_images, label_tensor


def current_dataset_name() -> str:
    return DATASET_NAME


def is_multilabel_dataset() -> bool:
    return current_dataset_name() == "chestmnist"


def chestmnist_training_metadata_path() -> Path:
    override = os.environ.get("CHESTMNIST_TRAINING_METADATA_PATH")
    if override:
        return Path(override).resolve()

    candidates = [
        data_dir() / "training-metadata.json",
        repo_root() / "data" / "chestmnist" / "training-metadata.json",
        Path("/dfl/config/chestmnist/training-metadata.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def multilabel_pos_weight_from_counts(
    sample_count: int,
    positive_counts: Iterable[int],
    *,
    cap: float = CHESTMNIST_DEFAULT_POS_WEIGHT_CAP,
    dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    positives = torch.tensor(list(positive_counts), dtype=dtype)
    if positives.numel() != CHESTMNIST_NUM_LABELS:
        raise ValueError(f"Expected {CHESTMNIST_NUM_LABELS} ChestMNIST positive counts, got {positives.numel()}")
    if sample_count <= 0 or torch.any(positives <= 0) or torch.any(positives >= sample_count):
        raise ValueError("ChestMNIST positive counts must be between zero and the sample count")
    if not np.isfinite(cap) or cap < 1.0:
        raise ValueError("DFL_POS_WEIGHT_CAP must be a finite number greater than or equal to one")

    negatives = float(sample_count) - positives
    return torch.clamp(negatives / positives, max=float(cap))


def load_chestmnist_pos_weight(*, dtype: torch.dtype = torch.float32) -> torch.Tensor:
    metadata_path = chestmnist_training_metadata_path()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (
        metadata.get("schema_version") != 1
        or metadata.get("dataset") != "chestmnist"
        or metadata.get("source_split") != "train"
        or metadata.get("label_count") != CHESTMNIST_NUM_LABELS
    ):
        raise ValueError(f"Unexpected ChestMNIST metadata in {metadata_path}")
    sample_count = int(metadata["sample_count"])
    positive_counts = [int(value) for value in metadata["positive_counts"]]
    negative_counts = [int(value) for value in metadata["negative_counts"]]
    if len(negative_counts) != CHESTMNIST_NUM_LABELS or any(
        positive + negative != sample_count
        for positive, negative in zip(positive_counts, negative_counts, strict=True)
    ):
        raise ValueError(f"Inconsistent ChestMNIST class counts in {metadata_path}")
    cap = _environment_float("DFL_POS_WEIGHT_CAP", CHESTMNIST_DEFAULT_POS_WEIGHT_CAP)
    return multilabel_pos_weight_from_counts(
        sample_count,
        positive_counts,
        cap=cap,
        dtype=dtype,
    )


def train_dataset_paths() -> tuple[Path, Path | None]:
    if is_multilabel_dataset():
        return input_data_file("train-data.npz"), None
    return input_data_file("train-images.idx3-ubyte"), input_data_file("train-labels.idx1-ubyte")


def test_dataset_paths() -> tuple[Path, Path | None]:
    if is_multilabel_dataset():
        return input_data_file("test-data.npz"), None
    return input_data_file("t10k-images.idx3-ubyte"), input_data_file("t10k-labels.idx1-ubyte")


def load_training_dataset() -> tuple[torch.Tensor, torch.Tensor]:
    train_data_path, train_labels_path = train_dataset_paths()
    if is_multilabel_dataset():
        images, labels = read_npz_images_and_labels(train_data_path)
        labels = labels.to(torch.float64).reshape(images.shape[0], OUTPUT_SIZE)
        return images, labels

    assert train_labels_path is not None
    num_images = read_idx_image_count(train_data_path)
    num_labels = read_idx_label_count(train_labels_path)
    if num_images != num_labels:
        raise ValueError(
            f"Mismatched training split sizes: {train_data_path} has {num_images} images "
            f"but {train_labels_path} has {num_labels} labels"
        )
    images = read_idx_images(train_data_path, limit=num_images)
    labels = read_idx_labels(train_labels_path, limit=num_labels)
    return images, labels


def load_test_dataset() -> tuple[torch.Tensor, torch.Tensor]:
    test_data_path, test_labels_path = test_dataset_paths()
    if is_multilabel_dataset():
        images, labels = read_npz_images_and_labels(test_data_path)
        labels = labels.to(torch.float64).reshape(images.shape[0], OUTPUT_SIZE)
        return images, labels

    assert test_labels_path is not None
    limit = min(10_000, test_labels_path.stat().st_size - 8)
    images = read_idx_images(test_data_path, limit=limit)
    labels = read_idx_labels(test_labels_path, limit=limit)
    return images, labels


def batch_accuracy_stats(logits: torch.Tensor, labels: torch.Tensor) -> tuple[int, int]:
    if is_multilabel_dataset():
        predictions = (torch.sigmoid(logits) >= 0.5).to(labels.dtype)
        correct = int((predictions == labels).sum().item())
        total = int(labels.numel())
        return correct, total

    correct = int((logits.argmax(dim=1) == labels).sum().item())
    total = int(labels.shape[0])
    return correct, total


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _safe_percent(numerator: float, denominator: float) -> float | None:
    ratio = _safe_ratio(numerator, denominator)
    return None if ratio is None else ratio * 100.0


def _mean_defined(values: Iterable[float | None]) -> float | None:
    defined = [float(value) for value in values if value is not None and np.isfinite(float(value))]
    if not defined:
        return None
    return float(sum(defined) / len(defined))


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, np.generic):
        return _json_ready(value.item())
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    return value


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and not np.isfinite(value):
        return ""
    if isinstance(value, np.generic):
        return _csv_value(value.item())
    return value


def _append_csv_rows(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    if not materialized:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in materialized:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_json_ready(row), sort_keys=True) + "\n")


def _write_sample_metrics_enabled() -> bool:
    value = os.environ.get("DFL_EVAL_WRITE_SAMPLE_METRICS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _binary_auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    y = labels.astype(np.int64).reshape(-1)
    s = scores.astype(np.float64).reshape(-1)
    positives = int(y.sum())
    negatives = int(y.size - positives)
    if positives == 0 or negatives == 0:
        return None

    order = np.argsort(s, kind="mergesort")
    sorted_scores = s[order]
    ranks = np.empty(y.size, dtype=np.float64)
    start = 0
    while start < y.size:
        end = start + 1
        while end < y.size and sorted_scores[end] == sorted_scores[start]:
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end

    pos_rank_sum = float(ranks[y == 1].sum())
    auc = (pos_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)
    return float(auc)


def _base_metric_row(
    *,
    round_id: int | None,
    source_round: int | None,
    participant_count: int | None,
    aggregated_model_count: int | None,
    expected_models: int | None,
) -> dict[str, Any]:
    return {
        "timestamp_unix_ms": int(time.time() * 1000),
        "experiment_id": os.environ.get("DFL_EXPERIMENT_ID", "local"),
        "dataset": current_dataset_name(),
        "round": round_id,
        "source_round": source_round,
        "participant_count": participant_count,
        "aggregated_model_count": aggregated_model_count,
        "expected_models": expected_models,
    }


def _summary_fieldnames() -> list[str]:
    return [
        "kind",
        "timestamp_unix_ms",
        "experiment_id",
        "dataset",
        "round",
        "source_round",
        "participant_count",
        "aggregated_model_count",
        "expected_models",
        "sample_count",
        "label_count",
        "metric_granularity",
        "accuracy_percent",
        "correct_predictions",
        "total_predictions",
        "exact_match_percent",
        "loss",
        "micro_f1",
        "macro_f1",
        "macro_auroc",
        "label_metrics_path",
        "sample_metrics_path",
    ]


def _label_fieldnames() -> list[str]:
    return [
        "kind",
        "timestamp_unix_ms",
        "experiment_id",
        "dataset",
        "round",
        "source_round",
        "participant_count",
        "aggregated_model_count",
        "expected_models",
        "label_index",
        "label_name",
        "sample_count",
        "positive_count",
        "negative_count",
        "true_positive",
        "true_negative",
        "false_positive",
        "false_negative",
        "accuracy_percent",
        "precision",
        "recall",
        "f1",
        "auroc",
    ]


def _sample_fieldnames() -> list[str]:
    return [
        "kind",
        "timestamp_unix_ms",
        "experiment_id",
        "dataset",
        "round",
        "source_round",
        "participant_count",
        "aggregated_model_count",
        "expected_models",
        "sample_index",
        "sample_accuracy_percent",
        "exact_match",
        "sample_loss",
        "true_label",
        "predicted_label",
        "true_class_probability",
        "positive_label_count",
        "predicted_positive_label_count",
        "mean_probability",
    ]


def _evaluate_multilabel(
    logits: torch.Tensor,
    labels: torch.Tensor,
    base: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    probabilities = torch.sigmoid(logits).detach().cpu().numpy()
    y_true = labels.detach().cpu().numpy().astype(np.int64)
    y_pred = (probabilities >= 0.5).astype(np.int64)
    correct_matrix = y_pred == y_true

    loss_matrix = F.binary_cross_entropy_with_logits(logits, labels, reduction="none").detach().cpu().numpy()
    sample_losses = loss_matrix.mean(axis=1)
    sample_accuracy = correct_matrix.mean(axis=1)
    exact_matches = correct_matrix.all(axis=1)

    label_rows: list[dict[str, Any]] = []
    tp_total = tn_total = fp_total = fn_total = 0
    for label_idx in range(y_true.shape[1]):
        truth = y_true[:, label_idx]
        pred = y_pred[:, label_idx]
        tp = int(((pred == 1) & (truth == 1)).sum())
        tn = int(((pred == 0) & (truth == 0)).sum())
        fp = int(((pred == 1) & (truth == 0)).sum())
        fn = int(((pred == 0) & (truth == 1)).sum())
        tp_total += tp
        tn_total += tn
        fp_total += fp
        fn_total += fn
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = (
            None
            if precision is None or recall is None or precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        )
        label_rows.append(
            {
                **base,
                "kind": "global_model_label_evaluation",
                "label_index": label_idx,
                "label_name": f"label_{label_idx}",
                "sample_count": int(truth.size),
                "positive_count": int(truth.sum()),
                "negative_count": int(truth.size - truth.sum()),
                "true_positive": tp,
                "true_negative": tn,
                "false_positive": fp,
                "false_negative": fn,
                "accuracy_percent": _safe_percent(tp + tn, truth.size),
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "auroc": _binary_auc(probabilities[:, label_idx], truth),
            }
        )

    micro_precision = _safe_ratio(tp_total, tp_total + fp_total)
    micro_recall = _safe_ratio(tp_total, tp_total + fn_total)
    micro_f1 = (
        None
        if micro_precision is None or micro_recall is None or micro_precision + micro_recall == 0
        else 2 * micro_precision * micro_recall / (micro_precision + micro_recall)
    )
    correct = int(correct_matrix.sum())
    total = int(correct_matrix.size)
    summary = {
        **base,
        "kind": "global_model_evaluation",
        "timestamp_unix_ms": base.get("timestamp_unix_ms"),
        "sample_count": int(y_true.shape[0]),
        "label_count": int(y_true.shape[1]),
        "metric_granularity": "label_wise_multilabel",
        "accuracy_percent": _safe_percent(correct, total),
        "correct_predictions": correct,
        "total_predictions": total,
        "exact_match_percent": _safe_percent(int(exact_matches.sum()), int(exact_matches.size)),
        "loss": float(sample_losses.mean()) if sample_losses.size else None,
        "micro_f1": micro_f1,
        "macro_f1": _mean_defined(row["f1"] for row in label_rows),
        "macro_auroc": _mean_defined(row["auroc"] for row in label_rows),
    }

    sample_rows: list[dict[str, Any]] = []
    if _write_sample_metrics_enabled():
        for sample_idx in range(y_true.shape[0]):
            sample_rows.append(
                {
                    **base,
                    "kind": "global_model_sample_evaluation",
                    "sample_index": sample_idx,
                    "sample_accuracy_percent": float(sample_accuracy[sample_idx] * 100.0),
                    "exact_match": int(exact_matches[sample_idx]),
                    "sample_loss": float(sample_losses[sample_idx]),
                    "positive_label_count": int(y_true[sample_idx].sum()),
                    "predicted_positive_label_count": int(y_pred[sample_idx].sum()),
                    "mean_probability": float(probabilities[sample_idx].mean()),
                }
            )

    return summary, label_rows, sample_rows


def _evaluate_multiclass(
    logits: torch.Tensor,
    labels: torch.Tensor,
    base: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    probabilities = torch.softmax(logits, dim=1).detach().cpu().numpy()
    y_true = labels.detach().cpu().numpy().astype(np.int64)
    y_pred = probabilities.argmax(axis=1).astype(np.int64)
    correct_mask = y_pred == y_true
    sample_losses = F.cross_entropy(logits, labels, reduction="none").detach().cpu().numpy()

    label_rows: list[dict[str, Any]] = []
    tp_total = tn_total = fp_total = fn_total = 0
    for label_idx in range(OUTPUT_SIZE):
        truth = (y_true == label_idx).astype(np.int64)
        pred = (y_pred == label_idx).astype(np.int64)
        tp = int(((pred == 1) & (truth == 1)).sum())
        tn = int(((pred == 0) & (truth == 0)).sum())
        fp = int(((pred == 1) & (truth == 0)).sum())
        fn = int(((pred == 0) & (truth == 1)).sum())
        tp_total += tp
        tn_total += tn
        fp_total += fp
        fn_total += fn
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = (
            None
            if precision is None or recall is None or precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        )
        label_rows.append(
            {
                **base,
                "kind": "global_model_label_evaluation",
                "label_index": label_idx,
                "label_name": str(label_idx),
                "sample_count": int(y_true.size),
                "positive_count": int(truth.sum()),
                "negative_count": int(truth.size - truth.sum()),
                "true_positive": tp,
                "true_negative": tn,
                "false_positive": fp,
                "false_negative": fn,
                "accuracy_percent": _safe_percent(tp + tn, truth.size),
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "auroc": _binary_auc(probabilities[:, label_idx], truth),
            }
        )

    micro_precision = _safe_ratio(tp_total, tp_total + fp_total)
    micro_recall = _safe_ratio(tp_total, tp_total + fn_total)
    micro_f1 = (
        None
        if micro_precision is None or micro_recall is None or micro_precision + micro_recall == 0
        else 2 * micro_precision * micro_recall / (micro_precision + micro_recall)
    )
    correct = int(correct_mask.sum())
    total = int(y_true.size)
    summary = {
        **base,
        "kind": "global_model_evaluation",
        "timestamp_unix_ms": base.get("timestamp_unix_ms"),
        "sample_count": total,
        "label_count": OUTPUT_SIZE,
        "metric_granularity": "sample_wise_multiclass",
        "accuracy_percent": _safe_percent(correct, total),
        "correct_predictions": correct,
        "total_predictions": total,
        "exact_match_percent": _safe_percent(correct, total),
        "loss": float(sample_losses.mean()) if sample_losses.size else None,
        "micro_f1": micro_f1,
        "macro_f1": _mean_defined(row["f1"] for row in label_rows),
        "macro_auroc": _mean_defined(row["auroc"] for row in label_rows),
    }

    sample_rows: list[dict[str, Any]] = []
    if _write_sample_metrics_enabled():
        for sample_idx in range(y_true.size):
            sample_rows.append(
                {
                    **base,
                    "kind": "global_model_sample_evaluation",
                    "sample_index": sample_idx,
                    "sample_accuracy_percent": 100.0 if correct_mask[sample_idx] else 0.0,
                    "exact_match": int(correct_mask[sample_idx]),
                    "sample_loss": float(sample_losses[sample_idx]),
                    "true_label": int(y_true[sample_idx]),
                    "predicted_label": int(y_pred[sample_idx]),
                    "true_class_probability": float(probabilities[sample_idx, y_true[sample_idx]]),
                }
            )

    return summary, label_rows, sample_rows


def _persist_evaluation_metrics(
    summary: dict[str, Any],
    label_rows: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    out_dir = evaluation_dir()
    summary_csv = out_dir / "global_model_round_summary.csv"
    summary_jsonl = out_dir / "global_model_round_summary.jsonl"
    label_csv = out_dir / "global_model_label_metrics.csv"
    sample_csv = out_dir / "global_model_sample_metrics.csv"

    persisted_summary = {
        **summary,
        "label_metrics_path": str(label_csv),
        "sample_metrics_path": str(sample_csv) if sample_rows else "",
    }
    _append_csv_rows(summary_csv, _summary_fieldnames(), [persisted_summary])
    _append_jsonl(summary_jsonl, persisted_summary)
    _append_csv_rows(label_csv, _label_fieldnames(), label_rows)
    if sample_rows:
        _append_csv_rows(sample_csv, _sample_fieldnames(), sample_rows)
    return persisted_summary


def deterministic_training_seed(round_id: int | None = None, device_id: str | int | None = None) -> int:
    base_seed = int(os.environ.get("DFL_TRAIN_SEED", "42"))
    parsed_round = _optional_int(round_id)
    round_component = 0 if parsed_round is None else parsed_round
    device_component = "" if device_id is None else str(device_id)
    seed_material = f"{base_seed}:{round_component}:{device_component}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "little") & ((1 << 63) - 1)


def _environment_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return default if not value else float(value)


def training_learning_rate(round_id: int | None = None) -> float:
    """Return the configured learning rate for the current source-model round."""
    default_lr = CHESTMNIST_DEFAULT_LEARNING_RATE if is_multilabel_dataset() else LEARNING_RATE
    base_learning_rate = _environment_float("DFL_TRAIN_LEARNING_RATE", default_lr)
    if not np.isfinite(base_learning_rate) or base_learning_rate <= 0:
        raise ValueError("DFL_TRAIN_LEARNING_RATE must be finite and greater than zero")

    schedule = os.environ.get("DFL_TRAIN_LR_SCHEDULE", "constant").strip().lower() or "constant"
    if schedule not in {"constant", "late_cosine"}:
        raise ValueError(
            f"Unsupported DFL_TRAIN_LR_SCHEDULE={schedule!r}; expected 'constant' or 'late_cosine'"
        )
    if schedule == "constant" or round_id is None:
        return base_learning_rate

    parsed_round = _optional_int(round_id)
    if parsed_round is None or parsed_round < 0:
        raise ValueError("round_id must be a non-negative integer for the late-cosine schedule")
    target_round = int(os.environ.get("ROUND", "0"))
    decay_start_round = int(os.environ.get("DFL_TRAIN_LR_DECAY_START_ROUND", "20"))
    final_factor = _environment_float("DFL_TRAIN_LR_FINAL_FACTOR", 0.25)
    final_source_round = target_round - 1
    if target_round < 2:
        raise ValueError("ROUND must be at least 2 for the late-cosine schedule")
    if decay_start_round < 0 or decay_start_round >= final_source_round:
        raise ValueError(
            "DFL_TRAIN_LR_DECAY_START_ROUND must be non-negative and smaller than ROUND - 1"
        )
    if not np.isfinite(final_factor) or not 0.0 < final_factor <= 1.0:
        raise ValueError("DFL_TRAIN_LR_FINAL_FACTOR must be finite and in (0, 1]")
    if parsed_round <= decay_start_round:
        return base_learning_rate

    progress = min(
        1.0,
        (parsed_round - decay_start_round) / (final_source_round - decay_start_round),
    )
    multiplier = final_factor + (1.0 - final_factor) * 0.5 * (
        1.0 + math.cos(math.pi * progress)
    )
    return base_learning_rate * multiplier


def build_training_optimizer(
    model: nn.Module,
    *,
    round_id: int | None = None,
) -> torch.optim.Optimizer:
    default_name = "adamw" if is_multilabel_dataset() else "sgd"
    name = os.environ.get("DFL_TRAIN_OPTIMIZER", "").strip().lower() or default_name
    learning_rate = training_learning_rate(round_id)
    weight_decay = _environment_float(
        "DFL_TRAIN_WEIGHT_DECAY",
        CHESTMNIST_DEFAULT_WEIGHT_DECAY if is_multilabel_dataset() else 0.0,
    )
    if not np.isfinite(weight_decay) or weight_decay < 0:
        raise ValueError("DFL_TRAIN_WEIGHT_DECAY must be finite and non-negative")

    if name == "adamw":
        return torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    if name == "sgd":
        momentum = _environment_float("DFL_TRAIN_MOMENTUM", 0.0)
        if not np.isfinite(momentum) or not 0 <= momentum < 1:
            raise ValueError("DFL_TRAIN_MOMENTUM must be finite and in [0, 1)")
        return torch.optim.SGD(
            model.parameters(),
            lr=learning_rate,
            momentum=momentum,
            weight_decay=weight_decay,
        )
    raise ValueError(f"Unsupported DFL_TRAIN_OPTIMIZER={name!r}; expected 'adamw' or 'sgd'")


def build_training_criterion(*, dtype: torch.dtype = torch.float32) -> nn.Module:
    if is_multilabel_dataset():
        return nn.BCEWithLogitsLoss(pos_weight=load_chestmnist_pos_weight(dtype=dtype))
    return nn.CrossEntropyLoss()


def train_model(
    epochs: int,
    aggregator_public_key_der_hex: str,
    medical_signer_snapshot: dict[str, Any] | None = None,
    *,
    private_key: str | None = None,
    round_id: int | None = None,
    device_id: str | int | None = None,
) -> None:
    if epochs <= 0:
        raise ValueError("epochs must be greater than zero")
    gm_path = data_dir() / "gm.bin"
    print(f"Starting local training from on-chain resolved global model: {gm_path}")
    model = read_model_bin(gm_path).float()
    if is_multilabel_dataset():
        if not medical_signer_snapshot:
            raise ValueError("signed ChestMNIST training requires an on-chain medical signer snapshot")
        train_data_path, _ = train_dataset_paths()
        provenance = verify_training_provenance(train_data_path, medical_signer_snapshot)
        print(f"Verified signed ChestMNIST provenance: {json.dumps(provenance, sort_keys=True)}")
    images, labels = load_training_dataset()
    images = images.float()
    labels = labels.float() if is_multilabel_dataset() else labels.long()
    num_images = int(images.shape[0])
    if num_images <= 0:
        raise ValueError("The local training shard is empty")
    optimizer = build_training_optimizer(model, round_id=round_id)
    criterion = build_training_criterion(dtype=images.dtype)
    grad_clip_norm = _environment_float(
        "DFL_GRAD_CLIP_NORM",
        CHESTMNIST_DEFAULT_GRAD_CLIP_NORM if is_multilabel_dataset() else 5.0,
    )
    if not np.isfinite(grad_clip_norm) or grad_clip_norm <= 0:
        raise ValueError("DFL_GRAD_CLIP_NORM must be finite and greater than zero")
    training_seed = deterministic_training_seed(round_id, device_id)
    generator = torch.Generator().manual_seed(training_seed)
    optimizer_group = optimizer.param_groups[0]
    print(
        f"Training configuration: optimizer={optimizer.__class__.__name__}, "
        f"lr={optimizer_group['lr']}, "
        f"lr_schedule={os.environ.get('DFL_TRAIN_LR_SCHEDULE', 'constant') or 'constant'}, "
        f"weight_decay={optimizer_group['weight_decay']}, "
        f"batch_size={BATCH_SIZE}, grad_clip_norm={grad_clip_norm}, "
        f"seed={training_seed}, samples={num_images}, "
        f"torch_threads={torch.get_num_threads()}, "
        f"torch_interop_threads={torch.get_num_interop_threads()}"
    )

    for epoch in range(1, epochs + 1):
        indices = torch.randperm(num_images, generator=generator)
        correct = 0
        total_predictions = 0
        predicted_positives = 0
        cumulative_loss = 0.0
        for start in range(0, num_images, BATCH_SIZE):
            batch_idx = indices[start : start + BATCH_SIZE]
            x = images.index_select(0, batch_idx)
            y = labels.index_select(0, batch_idx)
            logits = model(x)
            loss = criterion(logits, y)
            if not torch.isfinite(loss):
                raise FloatingPointError(f"Non-finite training loss in epoch {epoch}")
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                model.parameters(),
                max_norm=grad_clip_norm,
                error_if_nonfinite=True,
            )
            optimizer.step()
            batch_correct, batch_total = batch_accuracy_stats(logits, y)
            correct += batch_correct
            total_predictions += batch_total
            cumulative_loss += float(loss.detach().item()) * int(x.shape[0])
            if is_multilabel_dataset():
                predicted_positives += int((torch.sigmoid(logits.detach()) >= 0.5).sum().item())
        accuracy_percent = (correct / total_predictions) * 100 if total_predictions else 0.0
        print(f"Epoch: {epoch}/{epochs}")
        print(f"Mean loss: {cumulative_loss / num_images:.6f}")
        if is_multilabel_dataset():
            positive_rate = (predicted_positives / total_predictions) * 100 if total_predictions else 0.0
            print(
                f"Uncalibrated label-wise accuracy @0.5: "
                f"{accuracy_percent:.2f}% ({correct}/{total_predictions})"
            )
            print(f"Uncalibrated predicted-positive rate @0.5: {positive_rate:.2f}%")
        else:
            print(f"Accuracy: {accuracy_percent:.2f}% ({correct}/{total_predictions})")
        print()

    lm_path = data_dir() / "lm.bin"
    write_model_bin(model, lm_path)
    if aggregator_public_key_der_hex:
        encrypt_model_package(
            lm_path,
            Path(str(lm_path) + ".enc"),
            aggregator_public_key_der_hex,
            private_key=private_key,
        )
    print("Finished training!")


def load_private_key(path: Path):
    ensure_crypto()
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def load_public_key_from_der_hex(der_hex: str):
    ensure_crypto()
    text = der_hex.strip()
    if text.startswith(("0x", "0X")):
        text = text[2:]
    return serialization.load_der_public_key(bytes.fromhex("".join(text.split())))


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty padded data")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS7 padding")
    return data[:-pad_len]


def aes256_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(pkcs7_pad(data)) + encryptor.finalize()


def aes256_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    return pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())


def public_der_from_private_key(path: Path) -> bytes:
    private_key = load_private_key(path)
    return private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def encrypt_model_package(
    model_path: Path,
    out_path: Path,
    public_key_der_hex: str,
    *,
    private_key: str | None = None,
) -> None:
    ensure_crypto()
    plaintext = model_path.read_bytes()
    key = os.urandom(32)
    iv = os.urandom(16)
    ciphertext = aes256_cbc_encrypt(plaintext, key, iv)
    public_key = load_public_key_from_der_hex(public_key_der_hex)
    wrapped = public_key.encrypt(
        key + iv,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    sender_pub = public_der_from_private_key(private_key_path(private_key))
    package = (
        struct.pack("<I", len(sender_pub))
        + sender_pub
        + struct.pack("<I", len(wrapped))
        + wrapped
        + struct.pack("<I", len(ciphertext))
        + ciphertext
    )
    out_path.write_bytes(package)
    print(f"Encrypted package written: {out_path}")


def decrypt_model_package(package: bytes, private_key_file: Path) -> bytes:
    private_key = load_private_key(private_key_file)
    offset = 0
    sender_len = struct.unpack_from("<I", package, offset)[0]
    offset += 4 + sender_len
    wrapped_len = struct.unpack_from("<I", package, offset)[0]
    offset += 4
    wrapped = package[offset : offset + wrapped_len]
    offset += wrapped_len
    ct_len = struct.unpack_from("<I", package, offset)[0]
    offset += 4
    ciphertext = package[offset : offset + ct_len]
    key_iv = private_key.decrypt(
        wrapped,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    if len(key_iv) != 48:
        raise ValueError("invalid wrapped key/iv length")
    return aes256_cbc_decrypt(ciphertext, key_iv[:32], key_iv[32:])


def start_server(
    client_limit: int,
    key_path: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    ensure_zmq()
    received_models_dir().mkdir(parents=True, exist_ok=True)
    context = zmq.Context()
    sock = context.socket(zmq.REP)
    sock.setsockopt(zmq.RCVTIMEO, 1000)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind("tcp://*:5555")
    print(f"Server started with {client_limit} expected clients")
    count = 0
    try:
        while count < client_limit and not (stop_event and stop_event.is_set()):
            try:
                parts = sock.recv_multipart()
            except zmq.Again:
                continue

            if len(parts) == 2:
                filename = parts[0].decode("utf-8")
                package = parts[1]
            else:
                filename = parts[0].decode("utf-8")
                print(f"Receiving file: {filename}")
                sock.send_string("Filename OK")
                try:
                    package = sock.recv()
                except zmq.Again:
                    sock.send_string("Receive timed out")
                    continue

            print(f"Receiving file: {filename}")
            try:
                plain = decrypt_model_package(package, private_key_path(key_path))
                out_name = filename[:-4] + ".bin" if filename.endswith(".enc") else filename
                out_path = received_models_dir() / out_name
                out_path.write_bytes(plain)
                count += 1
                print(f"Saved decrypted model to {out_path}")
                sock.send_string("File received and decrypted")
            except Exception as exc:  # noqa: BLE001
                print(f"Error: decrypt failed for {filename}: {exc}", file=sys.stderr)
                sock.send_string("Decrypt failed")
    finally:
        sock.close()
        context.term()
        print("ZMQ server stopped")


def start_client(server_ip: str, device_id: str, timeout_ms: int | None = None) -> None:
    ensure_zmq()
    context = zmq.Context()
    sock = context.socket(zmq.REQ)
    timeout = int(timeout_ms or os.environ.get("MODEL_TRANSFER_TIMEOUT_MS", "20000"))
    sock.setsockopt(zmq.SNDTIMEO, timeout)
    sock.setsockopt(zmq.RCVTIMEO, timeout)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        sock.connect(f"tcp://{server_ip}:5555")
        filename = f"wb_client_{device_id}.enc"
        package = (data_dir() / "lm.bin.enc").read_bytes()
        sock.send_multipart([filename.encode("utf-8"), package])
        reply = sock.recv_string()
        print(f"Server: {reply}")
        if reply != "File received and decrypted":
            raise RuntimeError(f"model transfer failed: {reply}")
    except zmq.Again as exc:
        raise TimeoutError(f"model transfer to {server_ip}:5555 timed out after {timeout}ms") from exc
    finally:
        sock.close()
        context.term()


_CANONICAL_WORKER_MODEL_RE = re.compile(r"^wb_client_(0x[0-9a-f]{40})\.bin$")


def _normalize_bytes32(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a 32-byte hexadecimal string")
    normalized = value.strip().lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{label} must be a 32-byte hexadecimal string")
    return f"0x{normalized}"


def _canonical_aggregation_model_paths(paths: Iterable[Path]) -> list[tuple[str, Path]]:
    identified: list[tuple[bytes, str, Path]] = []
    seen_addresses: set[str] = set()
    for path in paths:
        match = _CANONICAL_WORKER_MODEL_RE.fullmatch(path.name)
        if match is None:
            raise ValueError(
                f"non-canonical aggregation input filename {path.name!r}; expected wb_client_0x<40-lowercase-hex>.bin"
            )
        address = match.group(1)
        if address in seen_addresses:
            raise ValueError(f"duplicate aggregation input address: {address}")
        seen_addresses.add(address)
        identified.append((bytes.fromhex(address[2:]), address, path))
    identified.sort(key=lambda item: item[0])
    return [(address, path) for _, address, path in identified]


def _medical_snapshot_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    required = ("registry_address", "block_number", "block_hash", "key_set_version")
    missing = [field for field in required if snapshot.get(field) in (None, "")]
    if missing:
        raise ValueError("medical_signer_snapshot lacks evidence fields: " + ", ".join(missing))
    return {field: snapshot[field] for field in required}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_verified_hybrid_validation(
    *,
    medical_signer_snapshot: dict[str, Any] | None,
    expected_validation_data_hash: Any,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, Any], str, str]:
    if not is_multilabel_dataset():
        raise ValueError("Hybrid-R aggregation is restricted to signed ChestMNIST validation data")
    if not isinstance(medical_signer_snapshot, dict) or not medical_signer_snapshot:
        raise ValueError("Hybrid-R aggregation requires an on-chain medical_signer_snapshot")
    _medical_snapshot_evidence(medical_signer_snapshot)

    expected_hash = _normalize_bytes32(
        expected_validation_data_hash,
        "expected_validation_data_hash",
    )
    path = validation_dataset_path()
    if not path.is_file():
        raise ValueError(f"Hybrid-R validation artifact is missing: {path}")

    actual_hash = f"0x{validation_semantic_sha256(path)}"
    if actual_hash != expected_hash:
        raise ValueError(f"Hybrid-R validation semantic hash mismatch: got {actual_hash}, expected {expected_hash}")

    provenance = verify_training_provenance(path, medical_signer_snapshot)
    if provenance.get("dataset_split") != VALIDATION_SPLIT_ID:
        raise ValueError(
            f"Hybrid-R validation split must be {VALIDATION_SPLIT_ID}, got {provenance.get('dataset_split')!r}"
        )
    images, labels = read_npz_images_and_labels(path)
    labels = labels.to(torch.float64)
    if labels.ndim != 2 or tuple(labels.shape) != (int(images.shape[0]), OUTPUT_SIZE):
        raise ValueError(
            f"Hybrid-R validation labels have shape {tuple(labels.shape)}, "
            f"expected ({int(images.shape[0])}, {OUTPUT_SIZE})"
        )
    if images.shape[0] < 1:
        raise ValueError("Hybrid-R validation artifact is empty")
    if not torch.isfinite(images).all() or not torch.isfinite(labels).all():
        raise ValueError("Hybrid-R validation tensors contain a non-finite value")

    # Detect an in-process replacement between provenance verification and use.
    post_verification_hash = f"0x{validation_semantic_sha256(path)}"
    if post_verification_hash != actual_hash:
        raise ValueError("Hybrid-R validation artifact changed while it was being verified")
    return images, labels, provenance, actual_hash, _file_sha256(path)


def _run_hybrid_aggregation(
    canonical_inputs: list[tuple[str, Path]],
    *,
    source_round: int | None,
    round_id: int | None,
    medical_signer_snapshot: dict[str, Any] | None,
    expected_algorithm_hash: Any,
    expected_validation_data_hash: Any,
    max_loss_increase_bps: Any,
) -> tuple[FederatedCNN, dict[str, Any]]:
    algorithm_hash = _normalize_bytes32(
        expected_algorithm_hash,
        "expected_algorithm_hash",
    )
    if algorithm_hash != HYBRID_R_V1_HASH:
        raise ValueError(f"Hybrid-R algorithm hash mismatch: got {algorithm_hash}, expected {HYBRID_R_V1_HASH}")
    if isinstance(max_loss_increase_bps, bool):
        raise ValueError("max_loss_increase_bps must be an integer")
    try:
        loss_gate_bps = int(max_loss_increase_bps)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_loss_increase_bps must be an integer") from exc
    if loss_gate_bps != DEFAULT_MAX_LOSS_INCREASE_BPS:
        raise ValueError(
            f"unsupported max_loss_increase_bps: got {loss_gate_bps}, expected {DEFAULT_MAX_LOSS_INCREASE_BPS}"
        )
    if source_round is None or source_round < 0:
        raise ValueError("Hybrid-R aggregation requires a non-negative source_round")

    validation_images, validation_labels, provenance, validation_hash, file_hash = _load_verified_hybrid_validation(
        medical_signer_snapshot=medical_signer_snapshot,
        expected_validation_data_hash=expected_validation_data_hash,
    )
    assert medical_signer_snapshot is not None

    parent_path = data_dir() / "gm.bin"
    if not parent_path.is_file():
        raise ValueError(f"Hybrid-R parent model is missing: {parent_path}")
    parent_model = read_model_bin(parent_path)
    client_models = [read_model_bin(path) for _, path in canonical_inputs]
    result = run_hybrid_r(
        parent_model,
        client_models,
        validation_images,
        validation_labels,
        layout=MODEL_LAYOUT,
        batch_size=BATCH_SIZE,
        max_loss_increase_bps=loss_gate_bps,
    )

    evidence: dict[str, Any] = {
        "version": HYBRID_R_EVIDENCE_VERSION,
        "algorithm_hash": algorithm_hash,
        "validation_data_hash": validation_hash,
        "validation_data_file_sha256": f"0x{file_hash}",
        "source_round": source_round,
        "round": round_id,
        "input_count": len(canonical_inputs),
        "input_workers": [address for address, _ in canonical_inputs],
        "input_model_sha256": [
            {"worker": address, "sha256": f"0x{_file_sha256(path)}"} for address, path in canonical_inputs
        ],
        "parent_model_sha256": f"0x{_file_sha256(parent_path)}",
        "parent_loss": result.parent_loss,
        "allowed_loss": result.decision.allowed_loss,
        "max_loss_increase_bps": loss_gate_bps,
        "candidate_order": [str(candidate["candidate"]) for candidate in result.candidate_scores],
        "candidate_scores": result.candidate_scores,
        "selected_candidate": result.decision.selected_candidate,
        "selected_loss": result.decision.selected_loss,
        "gate_passed": result.decision.gate_passed,
        "gate_reason": result.decision.reason,
        "output_kind": result.decision.output_kind,
        "validation_provenance": provenance,
        "medical_signer_snapshot": _medical_snapshot_evidence(medical_signer_snapshot),
    }
    return result.output_model, evidence


def aggregate(
    num_files: int | None = None,
    *,
    round_id: int | None = None,
    source_round: int | None = None,
    expected_models: int | None = None,
    participant_count: int | None = None,
    medical_signer_snapshot: dict[str, Any] | None = None,
    expected_algorithm_hash: str | None = None,
    expected_validation_data_hash: str | None = None,
    max_loss_increase_bps: int | None = None,
    private_key: str | None = None,
) -> dict[str, Any]:
    model_paths = list(aggregation_inputs_dir().glob("*.bin"))
    if num_files is not None and len(model_paths) != num_files:
        raise ValueError(
            "aggregate input-count mismatch: "
            f"found {len(model_paths)} staged model file(s), expected exactly {num_files}"
        )
    print(f"Aggregating {len(model_paths)} received model file(s).")

    round_id = _optional_int(round_id)
    source_round = _optional_int(source_round)
    expected_models = _optional_int(expected_models)
    participant_count = _optional_int(participant_count)
    if not model_paths:
        raise ValueError("aggregate requires at least one model")
    if expected_models is not None and len(model_paths) != expected_models:
        raise ValueError(
            f"aggregate expected-model-count mismatch: found {len(model_paths)}, expected {expected_models}"
        )
    canonical_inputs = _canonical_aggregation_model_paths(model_paths)
    output_model, aggregation_evidence = _run_hybrid_aggregation(
        canonical_inputs,
        source_round=source_round,
        round_id=round_id,
        medical_signer_snapshot=medical_signer_snapshot,
        expected_algorithm_hash=expected_algorithm_hash,
        expected_validation_data_hash=expected_validation_data_hash,
        max_loss_increase_bps=max_loss_increase_bps,
    )
    out_path = results_dir() / "aggregated.bin"
    write_model_bin(output_model, out_path)
    aggregation_evidence["output_model_sha256"] = f"0x{_file_sha256(out_path)}"
    evidence_path = results_dir() / "aggregated.hybrid-r.json"
    write_canonical_json(evidence_path, aggregation_evidence)
    sign_file(out_path, private_key_path(private_key))
    print(
        "Hybrid-R aggregation complete: "
        f"output_kind={aggregation_evidence['output_kind']}, "
        f"selected_candidate={aggregation_evidence['selected_candidate']}"
    )
    metrics = run_test(
        out_path,
        round_id=round_id,
        source_round=source_round,
        aggregated_model_count=len(model_paths),
        expected_models=expected_models,
        participant_count=participant_count,
    )
    return {
        "model_path": str(out_path),
        "metrics": metrics,
        "aggregation_evidence": aggregation_evidence,
        "aggregation_evidence_path": str(evidence_path),
    }


def sign_file(path: Path, key_file: Path) -> None:
    private_key = load_private_key(key_file)
    signature = private_key.sign(path.read_bytes(), padding.PKCS1v15(), hashes.SHA256())
    sig_path = Path(str(path) + ".sig")
    sig_path.write_bytes(signature)
    print(f"Global model signature written: {sig_path} (bytes={len(signature)})")


def run_test(
    model_path: Path,
    *,
    round_id: int | None = None,
    source_round: int | None = None,
    aggregated_model_count: int | None = None,
    expected_models: int | None = None,
    participant_count: int | None = None,
) -> dict[str, Any]:
    test_images, test_labels = test_dataset_paths()
    if not test_images.exists():
        return {}
    if test_labels is not None and not test_labels.exists():
        return {}
    model = read_model_bin(model_path)
    model.eval()
    images, labels = load_test_dataset()
    limit = int(images.shape[0])
    logits_by_batch: list[torch.Tensor] = []
    with torch.no_grad():
        for start in range(0, limit, BATCH_SIZE):
            batch_logits = model(images[start : start + BATCH_SIZE])
            logits_by_batch.append(batch_logits.detach().cpu())

    logits = torch.cat(logits_by_batch, dim=0)
    base = _base_metric_row(
        round_id=round_id,
        source_round=source_round,
        participant_count=participant_count,
        aggregated_model_count=aggregated_model_count,
        expected_models=expected_models,
    )
    if is_multilabel_dataset():
        summary, label_rows, sample_rows = _evaluate_multilabel(logits, labels, base)
    else:
        summary, label_rows, sample_rows = _evaluate_multiclass(logits, labels, base)

    persisted_summary = _persist_evaluation_metrics(summary, label_rows, sample_rows)
    correct = int(persisted_summary.get("correct_predictions") or 0)
    total_predictions = int(persisted_summary.get("total_predictions") or 0)
    accuracy_percent = float(persisted_summary.get("accuracy_percent") or 0.0)
    print(f"Test accuracy: {accuracy_percent:.2f}% ({correct}/{total_predictions})")
    if persisted_summary.get("macro_f1") is not None:
        print(f"Macro F1: {float(persisted_summary['macro_f1']):.4f}")
    if persisted_summary.get("macro_auroc") is not None:
        print(f"Macro AUROC: {float(persisted_summary['macro_auroc']):.4f}")
    print(json.dumps(_json_ready(persisted_summary), separators=(",", ":"), sort_keys=True))
    return persisted_summary


def save_random() -> None:
    write_model_bin(random_model(), data_dir() / "random_start.bin")
    print("Weights and biases saved to file")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server")
    server.add_argument("client_limit", type=int)
    server.add_argument("private_key", nargs="?")
    client = sub.add_parser("client")
    client.add_argument("server_ip")
    client.add_argument("device_id")
    train = sub.add_parser("train")
    train.add_argument("epochs", type=int)
    train.add_argument("aggregator_public_key_der_hex", nargs="?", default="")
    train.add_argument("--round-id", type=int)
    train.add_argument("--device-id")
    sub.add_parser("get_random_wb")
    aggregate_parser = sub.add_parser("aggregate")
    aggregate_parser.add_argument("num_files", type=int)
    aggregate_parser.add_argument("--round-id", type=int)
    aggregate_parser.add_argument("--source-round", type=int)
    aggregate_parser.add_argument("--expected-models", type=int)
    aggregate_parser.add_argument("--participant-count", type=int)
    sub.add_parser("simulate")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "server":
        start_server(args.client_limit, args.private_key)
    elif args.command == "client":
        start_client(args.server_ip, args.device_id)
    elif args.command == "train":
        train_model(
            args.epochs,
            args.aggregator_public_key_der_hex,
            round_id=args.round_id,
            device_id=args.device_id,
        )
    elif args.command == "get_random_wb":
        save_random()
    elif args.command == "aggregate":
        aggregate(
            args.num_files,
            round_id=args.round_id,
            source_round=args.source_round,
            expected_models=args.expected_models,
            participant_count=args.participant_count,
        )
    elif args.command == "simulate":
        raise NotImplementedError("simulate is not part of the Node runtime path yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
