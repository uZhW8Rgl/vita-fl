#!/usr/bin/env python3
"""
Create a one-image MNIST query dataset and EZKL input for a single inference.

By default the script selects a random correctly classified MNIST test image
for the selected model. It writes a one-sample IDX image/label pair, an EZKL
input.json, a portable PGM preview, and prediction metadata.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

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
from neural_network.cli import INPUT_SIZE, read_idx_images, read_idx_labels  # type: ignore
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


def predict(model_path: Path, normalized_image: "torch.Tensor") -> tuple[int, list[float], list[float]]:
    model = load_worker_model(model_path).eval()
    with torch.no_grad():
        logits = model(normalized_image.reshape(1, INPUT_SIZE)).reshape(-1)
        probabilities = torch.softmax(logits, dim=0)
    return (
        int(logits.argmax().item()),
        [float(value) for value in logits.tolist()],
        [float(value) for value in probabilities.tolist()],
    )


def select_index(
    model_path: Path,
    images_path: Path,
    labels_path: Path,
    preferred_index: int | None,
    search_limit: int,
) -> tuple[int, int, int, list[float], list[float]]:
    ensure_torch()
    if preferred_index is not None:
        images = read_idx_images(images_path, limit=preferred_index + 1)
        labels = read_idx_labels(labels_path, limit=preferred_index + 1)
        label = int(labels[preferred_index].item())
        predicted, logits, probabilities = predict(model_path, images[preferred_index])
        return preferred_index, label, predicted, logits, probabilities

    limit = min(search_limit, labels_path.stat().st_size - 8)
    images = read_idx_images(images_path, limit=limit)
    labels = read_idx_labels(labels_path, limit=limit)
    model = load_worker_model(model_path).eval()
    with torch.no_grad():
        logits_batch = model(images)
        predictions = logits_batch.argmax(dim=1)

    candidates: list[tuple[int, int, int, list[float], list[float]]] = []
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
                )
            )

    if not candidates:
        raise ValueError(f"No correctly classified image found in first {limit} samples")
    return random.SystemRandom().choice(candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a single-image MNIST inference query for EZKL.")
    parser.add_argument("--model", type=Path, default=default_model_path(), help="Model to use for prediction")
    parser.add_argument("--images", type=Path, default=Path("data/mnist/data/t10k-images.idx3-ubyte"))
    parser.add_argument("--labels", type=Path, default=Path("data/mnist/data/t10k-labels.idx1-ubyte"))
    parser.add_argument("--index", type=int, default=None, help="Use a specific MNIST test index")
    parser.add_argument("--search-limit", type=int, default=1000, help="Search range when --index is omitted")
    parser.add_argument("--out-dir", type=Path, default=Path("zk_inference/single_query"))
    parser.add_argument("--input-json", type=Path, default=Path("zk_inference/out/input.json"))
    args = parser.parse_args()

    index, label, predicted, logits, probabilities = select_index(
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
        "correct": predicted == label,
        "logits": logits,
        "probabilities": probabilities,
        "files": sample["files"],
    }
    metadata_file = args.out_dir / "prediction.json"
    metadata_file.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"Selected MNIST test index: {index}")
    print(f"Random selection: {args.index is None}")
    print(f"True label: {label}")
    print(f"Predicted label: {predicted}")
    print(f"Correct: {predicted == label}")
    print(f"Wrote {args.out_dir / 'single-image.idx3-ubyte'}")
    print(f"Wrote {args.out_dir / 'single-label.idx1-ubyte'}")
    print(f"Wrote {args.out_dir / 'single-image.pgm'}")
    print(f"Wrote {args.input_json}")
    print(f"Wrote {metadata_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
