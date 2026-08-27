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
from typing import Any, Iterable, List

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F


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
CHESTMNIST_DEFAULT_THRESHOLD_MAX_PREVALENCE_MULTIPLIER = 2.0
MODEL_TRANSFER_FILENAME = re.compile(
    r"^wb_client_(?P<device>0x[0-9a-fA-F]{40})_round_(?P<round>0|[1-9][0-9]*)\.enc$"
)


def parse_model_transfer_filename(
    filename: str,
    *,
    expected_round: int | None = None,
) -> tuple[str, int]:
    match = MODEL_TRANSFER_FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError("invalid model-transfer filename")
    submitted_round = int(match.group("round"))
    if expected_round is not None and submitted_round != expected_round:
        raise ValueError(f"model belongs to round {submitted_round}, expected round {expected_round}")
    return match.group("device").lower(), submitted_round


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


def read_model_bin(path: Path) -> FederatedCNN:
    model = FederatedCNN().double()
    blob = path.read_bytes()
    offset = 0
    state = {}
    for name, shape in MODEL_LAYOUT:
        tensor, offset = _read_tensor(blob, offset, shape)
        state[name] = tensor
    if offset != len(blob):
        raise ValueError(f"{path} contains {len(blob) - offset} trailing bytes")
    model.load_state_dict(state)
    model.train()
    return model


def tensor_to_save_bytes(tensor: torch.Tensor, shape: tuple[int, ...]) -> bytes:
    values = tensor.detach().cpu().to(torch.float64)
    if tuple(values.shape) != shape:
        raise ValueError(f"Tensor shape mismatch: got {tuple(values.shape)}, expected {shape}")
    if not torch.isfinite(values).all():
        raise ValueError("Refusing to serialize a model with non-finite parameters")
    flat = values.reshape(-1).tolist()
    return struct.pack("<" + "d" * len(flat), *flat)


def model_to_bytes(model: FederatedCNN) -> bytes:
    state = model.state_dict()
    return b"".join(tensor_to_save_bytes(state[name], shape) for name, shape in MODEL_LAYOUT)


def write_model_bin(model: FederatedCNN, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(model_to_bytes(model))


def random_model(seed: int | None = None) -> FederatedCNN:
    """Create a reproducible, fan-in-aware bootstrap model.

    ``FederatedCNN`` uses PyTorch's layer-specific default initialization.  The
    previous blanket ``[-0.5, 0.5]`` initialization saturated the hidden tanh
    layers before the first ChestMNIST update.
    """
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
    if not np.isfinite(images).all():
        raise ValueError(f"{path} contains non-finite image values")
    if not np.isfinite(labels).all() or not np.isin(labels, (0, 1)).all():
        raise ValueError(f"{path} contains non-binary or non-finite labels")

    flat_images = torch.from_numpy(images.reshape(images.shape[0], INPUT_SIZE)).to(torch.float64)
    normalized_images = ((flat_images - 127.5) / 127.5).contiguous()
    label_tensor = torch.from_numpy(labels)
    return normalized_images, label_tensor


def chestmnist_source_path() -> Path:
    override = os.environ.get("CHESTMNIST_SOURCE_PATH")
    if override:
        return Path(override).resolve()
    candidates = [
        repo_root() / "data" / "chestmnist" / "chestmnist.npz",
        Path("/dfl/config/chestmnist/chestmnist.npz"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_chestmnist_source_split(split: str) -> tuple[torch.Tensor, torch.Tensor]:
    if split not in {"train", "val", "test"}:
        raise ValueError(f"Unsupported ChestMNIST split: {split}")
    path = chestmnist_source_path()
    with np.load(path) as bundle:
        image_key = f"{split}_images"
        label_key = f"{split}_labels"
        if image_key not in bundle or label_key not in bundle:
            raise ValueError(f"{path} does not contain {image_key!r} and {label_key!r}")
        images = bundle[image_key]
        labels = bundle[label_key]
    if images.ndim == 4 and images.shape[-1] == 1:
        images = images[..., 0]
    if images.ndim != 3 or tuple(images.shape[1:]) != (IMAGE_HEIGHT, IMAGE_WIDTH):
        raise ValueError(f"{path}:{split} has unexpected image shape {images.shape}")
    if labels.shape != (images.shape[0], CHESTMNIST_NUM_LABELS):
        raise ValueError(f"{path}:{split} has unexpected label shape {labels.shape}")
    if not np.isfinite(images).all() or not np.isin(labels, (0, 1)).all():
        raise ValueError(f"{path}:{split} contains invalid image or label values")
    flat_images = torch.from_numpy(images.reshape(images.shape[0], INPUT_SIZE)).to(torch.float32)
    normalized_images = ((flat_images - 127.5) / 127.5).contiguous()
    return normalized_images, torch.from_numpy(labels).to(torch.float32)


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
        positive + negative != sample_count for positive, negative in zip(positive_counts, negative_counts, strict=True)
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


def _binary_f1(true_positive: int, false_positive: int, false_negative: int) -> float:
    denominator = 2 * true_positive + false_positive + false_negative
    if denominator == 0:
        return 0.0
    return float(2 * true_positive) / float(denominator)


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
    if path.exists() and path.stat().st_size > 0:
        with path.open("r", newline="", encoding="utf-8") as existing_handle:
            reader = csv.DictReader(existing_handle)
            existing_fieldnames = list(reader.fieldnames or [])
            if existing_fieldnames != fieldnames:
                existing_rows = list(reader)
                migration_path = path.with_suffix(path.suffix + ".schema-migration")
                with migration_path.open("w", newline="", encoding="utf-8") as migration_handle:
                    writer = csv.DictWriter(migration_handle, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    for existing_row in existing_rows:
                        writer.writerow(
                            {field: _csv_value(existing_row.get(field)) for field in fieldnames}
                        )
                os.replace(migration_path, path)
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


def _binary_average_precision(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """Return tie-aware average precision for one binary label."""
    y = labels.astype(np.int64).reshape(-1)
    s = scores.astype(np.float64).reshape(-1)
    if y.size != s.size or not np.isfinite(s).all() or not np.isin(y, (0, 1)).all():
        raise ValueError("scores and labels must be equally sized and finite")
    positives = int(y.sum())
    if positives == 0:
        return None

    order = np.argsort(-s, kind="mergesort")
    sorted_scores = s[order]
    sorted_labels = y[order]
    true_positives = 0
    false_positives = 0
    average_precision = 0.0
    start = 0
    while start < y.size:
        end = start + 1
        while end < y.size and sorted_scores[end] == sorted_scores[start]:
            end += 1
        group_positives = int(sorted_labels[start:end].sum())
        true_positives += group_positives
        false_positives += (end - start) - group_positives
        precision = true_positives / (true_positives + false_positives)
        average_precision += (group_positives / positives) * precision
        start = end
    return float(average_precision)


def best_f1_threshold(
    scores: np.ndarray,
    labels: np.ndarray,
    *,
    max_predicted_positive_multiplier: float | None = None,
) -> float:
    """Select a deterministic per-label threshold on validation predictions.

    The optional multiplier constrains the number of predicted positives relative
    to the validation prevalence. This keeps a single rare positive from selecting
    a very low threshold that marks a large fraction of the split as positive.
    """
    y = labels.astype(np.int64).reshape(-1)
    s = scores.astype(np.float64).reshape(-1)
    if (
        y.size == 0
        or y.size != s.size
        or not np.isfinite(s).all()
        or not np.isin(y, (0, 1)).all()
    ):
        raise ValueError("validation scores and labels must be equally sized and finite")
    if max_predicted_positive_multiplier is not None and (
        not np.isfinite(max_predicted_positive_multiplier)
        or max_predicted_positive_multiplier < 1.0
    ):
        raise ValueError("max_predicted_positive_multiplier must be finite and at least 1")
    positives = int(y.sum())
    if positives == 0:
        return 0.5
    max_predicted_positives = (
        y.size
        if max_predicted_positive_multiplier is None
        else min(y.size, int(math.ceil(positives * max_predicted_positive_multiplier)))
    )

    order = np.argsort(-s, kind="mergesort")
    sorted_scores = s[order]
    sorted_labels = y[order]
    true_positives = 0
    false_positives = 0
    candidates: list[tuple[float, float]] = []
    start = 0
    while start < y.size:
        end = start + 1
        while end < y.size and sorted_scores[end] == sorted_scores[start]:
            end += 1
        group_positives = int(sorted_labels[start:end].sum())
        true_positives += group_positives
        false_positives += (end - start) - group_positives
        false_negatives = positives - true_positives
        if end <= max_predicted_positives:
            candidates.append(
                (_binary_f1(true_positives, false_positives, false_negatives), float(sorted_scores[start]))
            )
        start = end

    if not candidates:
        return 0.5
    best_f1 = max(candidate[0] for candidate in candidates)
    tied_thresholds = [threshold for f1, threshold in candidates if abs(f1 - best_f1) <= 1e-12]
    return min(tied_thresholds, key=lambda threshold: (abs(threshold - 0.5), -threshold))


def calibrate_multilabel_thresholds(
    probabilities: np.ndarray,
    labels: np.ndarray,
    *,
    max_predicted_positive_multiplier: float | None = None,
) -> np.ndarray:
    if probabilities.shape != labels.shape or probabilities.ndim != 2:
        raise ValueError("validation probabilities and labels must have the same two-dimensional shape")
    if (
        not np.isfinite(probabilities).all()
        or np.any((probabilities < 0.0) | (probabilities > 1.0))
        or not np.isin(labels, (0, 1)).all()
    ):
        raise ValueError("validation probabilities or labels are invalid")
    return np.asarray(
        [
            best_f1_threshold(
                probabilities[:, index],
                labels[:, index],
                max_predicted_positive_multiplier=max_predicted_positive_multiplier,
            )
            for index in range(labels.shape[1])
        ],
        dtype=np.float64,
    )


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
        "micro_f1_at_0_5",
        "macro_f1",
        "macro_f1_at_0_5",
        "macro_auroc",
        "macro_auprc",
        "threshold_source",
        "threshold_max_prevalence_multiplier",
        "mean_decision_threshold",
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
        "f1_at_0_5",
        "decision_threshold",
        "auroc",
        "auprc",
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
    decision_thresholds: np.ndarray | None = None,
    *,
    threshold_source: str | None = None,
    threshold_max_prevalence_multiplier: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    probabilities = torch.sigmoid(logits).detach().cpu().numpy()
    y_true = labels.detach().cpu().numpy().astype(np.int64)
    if decision_thresholds is None:
        decision_thresholds = np.full(y_true.shape[1], 0.5, dtype=np.float64)
        effective_threshold_source = "fixed_0.5"
    else:
        decision_thresholds = np.asarray(decision_thresholds, dtype=np.float64).reshape(-1)
        if decision_thresholds.shape != (y_true.shape[1],):
            raise ValueError(f"Expected {y_true.shape[1]} decision thresholds")
        if not np.isfinite(decision_thresholds).all() or np.any(
            (decision_thresholds < 0.0) | (decision_thresholds > 1.0)
        ):
            raise ValueError("decision thresholds must be finite probabilities in [0, 1]")
        effective_threshold_source = threshold_source or "validation_f1"
        if not effective_threshold_source.strip():
            raise ValueError("threshold_source must not be empty")
    fixed_predictions = (probabilities >= 0.5).astype(np.int64)
    y_pred = (probabilities >= decision_thresholds.reshape(1, -1)).astype(np.int64)
    correct_matrix = y_pred == y_true

    loss_matrix = F.binary_cross_entropy_with_logits(logits, labels, reduction="none").detach().cpu().numpy()
    sample_losses = loss_matrix.mean(axis=1)
    sample_accuracy = correct_matrix.mean(axis=1)
    exact_matches = correct_matrix.all(axis=1)

    label_rows: list[dict[str, Any]] = []
    tp_total = fp_total = fn_total = 0
    fixed_tp_total = fixed_fp_total = fixed_fn_total = 0
    for label_idx in range(y_true.shape[1]):
        truth = y_true[:, label_idx]
        pred = y_pred[:, label_idx]
        fixed_pred = fixed_predictions[:, label_idx]
        tp = int(((pred == 1) & (truth == 1)).sum())
        tn = int(((pred == 0) & (truth == 0)).sum())
        fp = int(((pred == 1) & (truth == 0)).sum())
        fn = int(((pred == 0) & (truth == 1)).sum())
        tp_total += tp
        fp_total += fp
        fn_total += fn
        fixed_tp = int(((fixed_pred == 1) & (truth == 1)).sum())
        fixed_fp = int(((fixed_pred == 1) & (truth == 0)).sum())
        fixed_fn = int(((fixed_pred == 0) & (truth == 1)).sum())
        fixed_tp_total += fixed_tp
        fixed_fp_total += fixed_fp
        fixed_fn_total += fixed_fn
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = _binary_f1(tp, fp, fn)
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
                "f1_at_0_5": _binary_f1(fixed_tp, fixed_fp, fixed_fn),
                "decision_threshold": float(decision_thresholds[label_idx]),
                "auroc": _binary_auc(probabilities[:, label_idx], truth),
                "auprc": _binary_average_precision(probabilities[:, label_idx], truth),
            }
        )

    micro_f1 = _binary_f1(tp_total, fp_total, fn_total)
    fixed_micro_f1 = _binary_f1(fixed_tp_total, fixed_fp_total, fixed_fn_total)
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
        "micro_f1_at_0_5": fixed_micro_f1,
        "macro_f1": _mean_defined(row["f1"] for row in label_rows),
        "macro_f1_at_0_5": _mean_defined(row["f1_at_0_5"] for row in label_rows),
        "macro_auroc": _mean_defined(row["auroc"] for row in label_rows),
        "macro_auprc": _mean_defined(row["auprc"] for row in label_rows),
        "threshold_source": effective_threshold_source,
        "threshold_max_prevalence_multiplier": threshold_max_prevalence_multiplier,
        "mean_decision_threshold": float(decision_thresholds.mean()),
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
    tp_total = fp_total = fn_total = 0
    for label_idx in range(OUTPUT_SIZE):
        truth = (y_true == label_idx).astype(np.int64)
        pred = (y_pred == label_idx).astype(np.int64)
        tp = int(((pred == 1) & (truth == 1)).sum())
        tn = int(((pred == 0) & (truth == 0)).sum())
        fp = int(((pred == 1) & (truth == 0)).sum())
        fn = int(((pred == 0) & (truth == 1)).sum())
        tp_total += tp
        fp_total += fp
        fn_total += fn
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = _binary_f1(tp, fp, fn)
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

    micro_f1 = _binary_f1(tp_total, fp_total, fn_total)
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
    *,
    round_id: int | None = None,
    device_id: str | int | None = None,
) -> None:
    if epochs <= 0:
        raise ValueError("epochs must be greater than zero")
    gm_path = data_dir() / "gm.bin"
    print(f"Starting local training from on-chain resolved global model: {gm_path}")
    model = read_model_bin(gm_path).float()
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
        encrypt_model_package(lm_path, Path(str(lm_path) + ".enc"), aggregator_public_key_der_hex)
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


def encrypt_model_package(model_path: Path, out_path: Path, public_key_der_hex: str) -> None:
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
    sender_pub = public_der_from_private_key(private_key_path())
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
    expected_round: int | None = None,
) -> None:
    ensure_zmq()
    received_models_dir().mkdir(parents=True, exist_ok=True)
    context = zmq.Context()
    sock = context.socket(zmq.REP)
    sock.setsockopt(zmq.RCVTIMEO, 1000)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind("tcp://*:5555")
    expected_round = _optional_int(expected_round)
    if client_limit <= 0:
        raise ValueError("client_limit must be greater than zero")
    if expected_round is not None and expected_round < 0:
        raise ValueError("expected_round must be non-negative")
    round_description = "any round" if expected_round is None else f"round {expected_round}"
    print(f"Server started with {client_limit} expected clients for {round_description}")
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
            temporary_path: Path | None = None
            try:
                device_address, submitted_round = parse_model_transfer_filename(
                    filename,
                    expected_round=expected_round,
                )
                plain = decrypt_model_package(package, private_key_path(key_path))
                out_name = f"wb_client_{device_address}_round_{submitted_round}.bin"
                out_path = received_models_dir() / out_name
                is_new_submission = not out_path.exists()
                temporary_path = out_path.with_suffix(out_path.suffix + ".part")
                temporary_path.write_bytes(plain)
                read_model_bin(temporary_path)
                os.replace(temporary_path, out_path)
                if is_new_submission:
                    count += 1
                else:
                    print(f"Replaced duplicate submission without increasing the client count: {out_path}")
                print(f"Saved decrypted model to {out_path}")
                sock.send_string("File received and decrypted")
            except Exception as exc:  # noqa: BLE001
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                print(f"Rejected model package {filename}: {exc}", file=sys.stderr)
                sock.send_string(f"Rejected: {exc}")
    finally:
        sock.close()
        context.term()
        print("ZMQ server stopped")


def start_client(
    server_ip: str,
    device_id: str,
    timeout_ms: int | None = None,
    round_id: int | None = None,
) -> None:
    ensure_zmq()
    context = zmq.Context()
    sock = context.socket(zmq.REQ)
    timeout = int(timeout_ms or os.environ.get("MODEL_TRANSFER_TIMEOUT_MS", "20000"))
    sock.setsockopt(zmq.SNDTIMEO, timeout)
    sock.setsockopt(zmq.RCVTIMEO, timeout)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        parsed_round = _optional_int(round_id)
        if parsed_round is None or parsed_round < 0:
            raise ValueError("round_id must be a non-negative integer for model transfer")
        normalized_device_id = str(device_id).lower()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", normalized_device_id):
            raise ValueError("device_id must be an Ethereum address for model transfer")
        sock.connect(f"tcp://{server_ip}:5555")
        filename = f"wb_client_{normalized_device_id}_round_{parsed_round}.enc"
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


def aggregate(
    num_files: int | None = None,
    *,
    round_id: int | None = None,
    source_round: int | None = None,
    expected_models: int | None = None,
    participant_count: int | None = None,
) -> dict[str, Any]:
    model_paths = sorted(received_models_dir().glob("*.bin"), key=lambda p: p.name)
    if num_files is not None and len(model_paths) != num_files:
        print(f"Aggregating {len(model_paths)} received model file(s), expected {num_files}.")
    else:
        print(f"Aggregating {len(model_paths)} received model file(s).")

    round_id = _optional_int(round_id)
    source_round = _optional_int(source_round)
    expected_models = _optional_int(expected_models)
    participant_count = _optional_int(participant_count)
    models: List[FederatedCNN] = [read_model_bin(path) for path in model_paths]
    if not models:
        raise ValueError("aggregate requires at least one model")
    avg_model = FederatedCNN().double()
    avg_state = {}
    for key in avg_model.state_dict().keys():
        avg_state[key] = torch.stack([m.state_dict()[key] for m in models]).mean(dim=0)
    avg_model.load_state_dict(avg_state)
    out_path = results_dir() / "aggregated.bin"
    write_model_bin(avg_model, out_path)
    sign_file(out_path, private_key_path())
    print("Federated averaging complete")
    metrics = run_test(
        out_path,
        round_id=round_id,
        source_round=source_round,
        aggregated_model_count=len(model_paths),
        expected_models=expected_models,
        participant_count=participant_count,
    )
    return {"model_path": str(out_path), "metrics": metrics}


def sign_file(path: Path, key_file: Path) -> None:
    private_key = load_private_key(key_file)
    signature = private_key.sign(path.read_bytes(), padding.PKCS1v15(), hashes.SHA256())
    sig_path = Path(str(path) + ".sig")
    sig_path.write_bytes(signature)
    print(f"Global model signature written: {sig_path} (bytes={len(signature)})")


def predict_logits(model: nn.Module, images: torch.Tensor) -> torch.Tensor:
    logits_by_batch: list[torch.Tensor] = []
    with torch.no_grad():
        for start in range(0, int(images.shape[0]), BATCH_SIZE):
            batch_logits = model(images[start : start + BATCH_SIZE])
            logits_by_batch.append(batch_logits.detach().cpu())
    if not logits_by_batch:
        raise ValueError("evaluation split is empty")
    return torch.cat(logits_by_batch, dim=0)


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
    model = read_model_bin(model_path).float()
    model.eval()
    images, labels = load_test_dataset()
    images = images.float()
    labels = labels.float() if is_multilabel_dataset() else labels.long()
    logits = predict_logits(model, images)
    base = _base_metric_row(
        round_id=round_id,
        source_round=source_round,
        participant_count=participant_count,
        aggregated_model_count=aggregated_model_count,
        expected_models=expected_models,
    )
    if is_multilabel_dataset():
        validation_images, validation_labels = load_chestmnist_source_split("val")
        validation_logits = predict_logits(model, validation_images.float())
        validation_probabilities = torch.sigmoid(validation_logits).numpy()
        threshold_max_prevalence_multiplier = _environment_float(
            "DFL_THRESHOLD_MAX_PREVALENCE_MULTIPLIER",
            CHESTMNIST_DEFAULT_THRESHOLD_MAX_PREVALENCE_MULTIPLIER,
        )
        if (
            not np.isfinite(threshold_max_prevalence_multiplier)
            or threshold_max_prevalence_multiplier < 1.0
        ):
            raise ValueError(
                "DFL_THRESHOLD_MAX_PREVALENCE_MULTIPLIER must be finite and at least 1"
            )
        decision_thresholds = calibrate_multilabel_thresholds(
            validation_probabilities,
            validation_labels.numpy().astype(np.int64),
            max_predicted_positive_multiplier=threshold_max_prevalence_multiplier,
        )
        summary, label_rows, sample_rows = _evaluate_multilabel(
            logits,
            labels,
            base,
            decision_thresholds,
            threshold_source="validation_f1_prevalence_capped",
            threshold_max_prevalence_multiplier=threshold_max_prevalence_multiplier,
        )
    else:
        summary, label_rows, sample_rows = _evaluate_multiclass(logits, labels, base)

    persisted_summary = _persist_evaluation_metrics(summary, label_rows, sample_rows)
    correct = int(persisted_summary.get("correct_predictions") or 0)
    total_predictions = int(persisted_summary.get("total_predictions") or 0)
    accuracy_percent = float(persisted_summary.get("accuracy_percent") or 0.0)
    print(f"Test accuracy: {accuracy_percent:.2f}% ({correct}/{total_predictions})")
    if persisted_summary.get("macro_f1") is not None:
        print(f"Macro F1 (validation-calibrated thresholds): {float(persisted_summary['macro_f1']):.4f}")
    if persisted_summary.get("macro_f1_at_0_5") is not None:
        print(f"Macro F1 (fixed threshold 0.5, diagnostic): {float(persisted_summary['macro_f1_at_0_5']):.4f}")
    if persisted_summary.get("macro_auroc") is not None:
        print(f"Macro AUROC: {float(persisted_summary['macro_auroc']):.4f}")
    if persisted_summary.get("macro_auprc") is not None:
        print(f"Macro AUPRC: {float(persisted_summary['macro_auprc']):.4f}")
    print(json.dumps(_json_ready(persisted_summary), separators=(",", ":"), sort_keys=True))
    return persisted_summary


def save_random(output_path: Path | None = None) -> None:
    destination = output_path or (data_dir() / "random_start.bin")
    write_model_bin(random_model(), destination)
    print(f"Weights and biases saved to {destination}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server")
    server.add_argument("client_limit", type=int)
    server.add_argument("private_key", nargs="?")
    server.add_argument("--expected-round", type=int)
    client = sub.add_parser("client")
    client.add_argument("server_ip")
    client.add_argument("device_id")
    client.add_argument("--round-id", type=int, required=True)
    train = sub.add_parser("train")
    train.add_argument("epochs", type=int)
    train.add_argument("aggregator_public_key_der_hex", nargs="?", default="")
    train.add_argument("--round-id", type=int)
    train.add_argument("--device-id")
    random_parser = sub.add_parser("get_random_wb")
    random_parser.add_argument("--output", type=Path)
    aggregate_parser = sub.add_parser("aggregate")
    aggregate_parser.add_argument("num_files", type=int)
    aggregate_parser.add_argument("--round-id", type=int)
    aggregate_parser.add_argument("--source-round", type=int)
    aggregate_parser.add_argument("--expected-models", type=int)
    aggregate_parser.add_argument("--participant-count", type=int)
    sub.add_parser("simulate")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "server":
        start_server(args.client_limit, args.private_key, expected_round=args.expected_round)
    elif args.command == "client":
        start_client(args.server_ip, args.device_id, round_id=args.round_id)
    elif args.command == "train":
        train_model(
            args.epochs,
            args.aggregator_public_key_der_hex,
            round_id=args.round_id,
            device_id=args.device_id,
        )
    elif args.command == "get_random_wb":
        save_random(args.output)
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
