#!/usr/bin/env python3
"""
Create a one-image query dataset and EZKL input for a single inference.

For MNIST, the helper prefers a random correctly classified test image.
For ChestMNIST, it falls back to a random sample because exact multi-label
matches are much rarer. It writes a one-sample artifact, an EZKL input.json,
a portable PGM preview, and prediction metadata.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

try:
    import torch
except ImportError:  # pragma: no cover - handled at runtime
    torch = None


REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT / "dfl", REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))
if str(REPO_ROOT / "zk_inference") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "zk_inference"))

from data.mnist_tools import extract_mnist_sample  # type: ignore
from neural_network.cli import (  # type: ignore
    INPUT_SIZE,
    current_dataset_name,
    is_multilabel_dataset,
    read_idx_images,
    read_idx_labels,
    read_npz_images_and_labels,
)
from export_model import default_model_path, load_worker_model  # type: ignore


def metadata_path(path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def ensure_torch() -> None:
    if torch is None:
        raise RuntimeError("PyTorch is required. Install it with: pip install torch")


def predict_single_label(model_path: Path, normalized_image: "torch.Tensor") -> tuple[int, list[float], list[float]]:
    model = load_worker_model(model_path).eval()
    with torch.no_grad():
        logits = model(normalized_image.reshape(1, INPUT_SIZE)).reshape(-1)
        probabilities = torch.softmax(logits, dim=0)
    return (
        int(logits.argmax().item()),
        [float(value) for value in logits.tolist()],
        [float(value) for value in probabilities.tolist()],
    )


def predict_multilabel(
    model_path: Path,
    normalized_image: "torch.Tensor",
) -> tuple[list[int], list[float], list[float]]:
    model = load_worker_model(model_path).eval()
    with torch.no_grad():
        logits = model(normalized_image.reshape(1, INPUT_SIZE)).reshape(-1)
        probabilities = torch.sigmoid(logits)
        predictions = (probabilities >= 0.5).to(torch.int64)
    return (
        [int(value) for value in predictions.tolist()],
        [float(value) for value in logits.tolist()],
        [float(value) for value in probabilities.tolist()],
    )


def select_index(
    model_path: Path,
    images_path: Path,
    labels_path: Path,
    preferred_index: int | None,
    search_limit: int,
) -> tuple[int, Any, Any, list[float], list[float], bool]:
    ensure_torch()
    if is_multilabel_dataset():
        images, labels = read_npz_images_and_labels(images_path)
        labels = labels.reshape(labels.shape[0], -1)
        limit = min(search_limit, int(images.shape[0]))
        if preferred_index is not None:
            true_labels = [int(value) for value in labels[preferred_index].reshape(-1).tolist()]
            predicted, logits, probabilities = predict_multilabel(model_path, images[preferred_index])
            return preferred_index, true_labels, predicted, logits, probabilities, predicted == true_labels

        if limit <= 0:
            raise ValueError(f"No samples available in {images_path}")
        index = random.SystemRandom().randrange(limit)
        true_labels = [int(value) for value in labels[index].reshape(-1).tolist()]
        predicted, logits, probabilities = predict_multilabel(model_path, images[index])
        return index, true_labels, predicted, logits, probabilities, predicted == true_labels

    if preferred_index is not None:
        images = read_idx_images(images_path, limit=preferred_index + 1)
        labels = read_idx_labels(labels_path, limit=preferred_index + 1)
        label = int(labels[preferred_index].item())
        predicted, logits, probabilities = predict_single_label(model_path, images[preferred_index])
        return preferred_index, label, predicted, logits, probabilities, predicted == label

    limit = min(search_limit, labels_path.stat().st_size - 8)
    images = read_idx_images(images_path, limit=limit)
    labels = read_idx_labels(labels_path, limit=limit)
    model = load_worker_model(model_path).eval()
    with torch.no_grad():
        logits_batch = model(images)
        predictions = logits_batch.argmax(dim=1)

    candidates: list[tuple[int, int, int, list[float], list[float], bool]] = []
    for index in range(limit):
        label = int(labels[index].item())
        predicted = int(predictions[index].item())
        if predicted == label:
            logits = logits_batch[index]
            probabilities = torch.softmax(logits, dim=0)
            candidates.append(
                (
                    index,
                    label,
                    predicted,
                    [float(value) for value in logits.tolist()],
                    [float(value) for value in probabilities.tolist()],
                    True,
                )
            )

    if not candidates:
        raise ValueError(f"No correctly classified image found in first {limit} samples")
    return random.SystemRandom().choice(candidates)


def main() -> int:
    dataset = current_dataset_name()
    default_images = (
        Path("data/chestmnist/test_data/test-data.npz")
        if dataset == "chestmnist"
        else Path("data/mnist/data/t10k-images.idx3-ubyte")
    )
    default_labels = (
        Path("data/chestmnist/test_data/test-data.npz")
        if dataset == "chestmnist"
        else Path("data/mnist/data/t10k-labels.idx1-ubyte")
    )
    parser = argparse.ArgumentParser(description="Create a single-image inference query for EZKL.")
    parser.add_argument("--model", type=Path, default=default_model_path(), help="Model to use for prediction")
    parser.add_argument("--images", type=Path, default=default_images)
    parser.add_argument("--labels", type=Path, default=default_labels)
    parser.add_argument("--index", type=int, default=None, help="Use a specific test index")
    parser.add_argument("--search-limit", type=int, default=1000, help="Search range when --index is omitted")
    parser.add_argument("--out-dir", type=Path, default=Path("zk_inference/single_query"))
    parser.add_argument("--input-json", type=Path, default=Path("zk_inference/out/input.json"))
    args = parser.parse_args()

    index, label, predicted, logits, probabilities, correct = select_index(
        model_path=args.model,
        images_path=args.images,
        labels_path=args.labels,
        preferred_index=args.index,
        search_limit=args.search_limit,
    )
    sample = extract_mnist_sample(
        images_path=args.images,
        labels_path=args.labels,
        index=index,
        out_dir=args.out_dir,
        input_json=args.input_json,
    )

    metadata = {
        "source_model": metadata_path(args.model),
        "source_images": sample["source_images"],
        "source_labels": sample["source_labels"],
        "source_index": index,
        "true_label": label,
        "predicted_label": predicted,
        "correct": correct,
        "logits": logits,
        "probabilities": probabilities,
        "dataset": dataset,
        "files": sample["files"],
    }
    metadata_file = args.out_dir / "prediction.json"
    metadata_file.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"Selected {dataset} test index: {index}")
    print(f"Random selection: {args.index is None}")
    print(f"True label: {label}")
    print(f"Predicted label: {predicted}")
    print(f"Correct: {correct}")
    print(f"Wrote {args.out_dir / 'single-image.pgm'}")
    print(f"Wrote {args.input_json}")
    print(f"Wrote {metadata_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
