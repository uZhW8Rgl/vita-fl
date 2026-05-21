from __future__ import annotations

import argparse
import random
import struct
from pathlib import Path


INPUT_SIZE = 28 * 28
IMAGE_MAGIC = 2051
LABEL_MAGIC = 2049


def read_idx_images(path: Path) -> bytes:
    blob = path.read_bytes()
    if len(blob) < 16:
        raise ValueError(f"{path} is too small to be a valid IDX image file")
    magic, count, rows, cols = struct.unpack(">IIII", blob[:16])
    if magic != IMAGE_MAGIC:
        raise ValueError(f"{path} has unexpected image magic {magic}")
    if rows * cols != INPUT_SIZE:
        raise ValueError(f"{path} has unexpected image dimensions {rows}x{cols}")
    payload = blob[16:]
    if len(payload) != count * INPUT_SIZE:
        raise ValueError(f"{path} payload size does not match header count")
    return payload


def read_idx_labels(path: Path) -> bytes:
    blob = path.read_bytes()
    if len(blob) < 8:
        raise ValueError(f"{path} is too small to be a valid IDX label file")
    magic, count = struct.unpack(">II", blob[:8])
    if magic != LABEL_MAGIC:
        raise ValueError(f"{path} has unexpected label magic {magic}")
    payload = blob[8:]
    if len(payload) != count:
        raise ValueError(f"{path} payload size does not match header count")
    return payload


def write_idx_images(path: Path, images: bytes, count: int) -> None:
    header = struct.pack(">IIII", IMAGE_MAGIC, count, 28, 28)
    path.write_bytes(header + images)


def write_idx_labels(path: Path, labels: bytes, count: int) -> None:
    header = struct.pack(">II", LABEL_MAGIC, count)
    path.write_bytes(header + labels)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate reproducible MNIST worker training splits.")
    parser.add_argument(
        "--images",
        type=Path,
        default=Path("data/mnist/data/train-images.idx3-ubyte"),
        help="Source MNIST training images IDX file",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("data/mnist/data/train-labels.idx1-ubyte"),
        help="Source MNIST training labels IDX file",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/mnist/training_data"),
        help="Directory for per-worker training splits",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Number of worker splits to create",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Shuffle seed for reproducible IID partitioning",
    )
    args = parser.parse_args()

    image_payload = read_idx_images(args.images)
    label_payload = read_idx_labels(args.labels)
    total = len(label_payload)
    if len(image_payload) != total * INPUT_SIZE:
        raise ValueError("Image and label counts do not match")
    if total < args.workers:
        raise ValueError(f"Cannot create {args.workers} splits from only {total} examples")

    indices = list(range(total))
    random.Random(args.seed).shuffle(indices)
    base = total // args.workers
    remainder = total % args.workers

    args.out_dir.mkdir(parents=True, exist_ok=True)
    start = 0
    for worker_id in range(args.workers):
        size = base + (1 if worker_id < remainder else 0)
        shard_indices = indices[start : start + size]
        start += size

        images = bytearray()
        labels = bytearray()
        for idx in shard_indices:
            offset = idx * INPUT_SIZE
            images.extend(image_payload[offset : offset + INPUT_SIZE])
            labels.append(label_payload[idx])

        write_idx_images(args.out_dir / f"train-images-{worker_id}.idx3-ubyte", bytes(images), size)
        write_idx_labels(args.out_dir / f"train-labels-{worker_id}.idx1-ubyte", bytes(labels), size)
        print(f"worker {worker_id}: {size} samples")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
