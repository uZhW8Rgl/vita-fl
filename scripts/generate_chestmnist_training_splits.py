from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np


def load_split(bundle: np.lib.npyio.NpzFile, images_key: str, labels_key: str) -> tuple[np.ndarray, np.ndarray]:
    if images_key not in bundle or labels_key not in bundle:
        raise ValueError(f"Missing {images_key!r} or {labels_key!r} in bundle")
    images = bundle[images_key]
    labels = bundle[labels_key]
    if images.shape[0] != labels.shape[0]:
        raise ValueError(f"Mismatched split sizes for {images_key} and {labels_key}")
    return images, labels


def write_npz(path: Path, images: np.ndarray, labels: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, images=images, labels=labels)


def write_training_metadata(path: Path, labels: np.ndarray) -> None:
    flattened = labels.reshape(labels.shape[0], -1)
    if flattened.shape[1] != 14:
        raise ValueError(f"Expected 14 ChestMNIST labels, got {flattened.shape[1]}")
    if not np.isfinite(flattened).all() or not np.isin(flattened, (0, 1)).all():
        raise ValueError("ChestMNIST labels must be finite binary values")
    positive_counts = flattened.astype(np.int64).sum(axis=0)
    sample_count = int(flattened.shape[0])
    payload = {
        "schema_version": 1,
        "dataset": "chestmnist",
        "source_split": "train",
        "sample_count": sample_count,
        "label_count": int(flattened.shape[1]),
        "positive_counts": [int(value) for value in positive_counts],
        "negative_counts": [int(sample_count - value) for value in positive_counts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate reproducible ChestMNIST worker training splits.")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/chestmnist/chestmnist.npz"),
        help="Official ChestMNIST .npz bundle",
    )
    parser.add_argument(
        "--train-out-dir",
        type=Path,
        default=Path("data/chestmnist/training_data"),
        help="Directory for per-worker training splits",
    )
    parser.add_argument(
        "--test-out",
        type=Path,
        default=Path("data/chestmnist/test_data/test-data.npz"),
        help="Destination for the shared test split",
    )
    parser.add_argument(
        "--metadata-out",
        type=Path,
        default=Path("data/chestmnist/training-metadata.json"),
        help="Destination for task-wide training-label statistics",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=25,
        help="Number of worker splits to create",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Shuffle seed for reproducible IID partitioning",
    )
    args = parser.parse_args()

    if not args.source.exists():
        raise FileNotFoundError(
            f"ChestMNIST source file not found: {args.source}\n"
            "Download the official bundle to data/chestmnist/chestmnist.npz first, for example with:\n"
            "  .venv/bin/python scripts/download_chestmnist.py"
        )

    with np.load(args.source) as bundle:
        train_images, train_labels = load_split(bundle, "train_images", "train_labels")
        test_images, test_labels = load_split(bundle, "test_images", "test_labels")

    total = int(train_images.shape[0])
    if total < args.workers:
        raise ValueError(f"Cannot create {args.workers} splits from only {total} training examples")

    indices = list(range(total))
    random.Random(args.seed).shuffle(indices)
    base = total // args.workers
    remainder = total % args.workers

    args.train_out_dir.mkdir(parents=True, exist_ok=True)
    start = 0
    for worker_id in range(args.workers):
        size = base + (1 if worker_id < remainder else 0)
        shard_indices = np.array(indices[start : start + size], dtype=np.int64)
        start += size
        write_npz(
            args.train_out_dir / f"train-data-{worker_id}.npz",
            train_images[shard_indices],
            train_labels[shard_indices],
        )
        print(f"worker {worker_id}: {size} samples")

    stale_shards = []
    for path in args.train_out_dir.glob("train-data-*.npz"):
        worker_id = path.stem.removeprefix("train-data-")
        if worker_id.isdigit() and int(worker_id) >= args.workers:
            stale_shards.append(path)
    for path in sorted(stale_shards):
        path.unlink()
    if stale_shards:
        print(f"removed {len(stale_shards)} stale worker shards")

    write_npz(args.test_out, test_images, test_labels)
    print(f"wrote test split: {args.test_out}")
    write_training_metadata(args.metadata_out, train_labels)
    print(f"wrote training metadata: {args.metadata_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
