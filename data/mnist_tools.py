#!/usr/bin/env python3
"""Helpers and CLI for extracting single-image samples for local inference flows."""

from __future__ import annotations

import argparse
import json
import random
import struct
import sys
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT / "dfl", REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from neural_network.cli import (  # type: ignore
    INPUT_SIZE,
    current_dataset_name,
    is_multilabel_dataset,
    read_idx_images,
    read_idx_labels,
    read_npz_images_and_labels,
)


IMAGE_ROWS = 28
IMAGE_COLS = 28


def metadata_path(path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def dataset_size(images_path: Path) -> int:
    if is_multilabel_dataset():
        images, _ = read_npz_images_and_labels(images_path)
        return int(images.shape[0])
    return (images_path.stat().st_size - 16) // INPUT_SIZE


def choose_random_index(images_path: Path, seed: int | None = None) -> int:
    total = dataset_size(images_path)
    if total <= 0:
        raise ValueError(f"No images found in {images_path}")
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    return int(rng.randrange(total))


def load_image(images_path: Path, index: int) -> list[float]:
    if is_multilabel_dataset():
        images, _ = read_npz_images_and_labels(images_path)
        return [float(value) for value in images[index].tolist()]
    images = read_idx_images(images_path, limit=index + 1)
    return [float(value) for value in images[index].tolist()]


def read_raw_image_bytes(images_path: Path, index: int) -> bytes:
    if is_multilabel_dataset():
        with np.load(images_path) as bundle:
            images = bundle["images"]
        raw = images[index]
        if raw.ndim == 3 and raw.shape[-1] == 1:
            raw = raw[..., 0]
        if raw.shape != (IMAGE_ROWS, IMAGE_COLS):
            raise ValueError(f"Unexpected image shape {raw.shape} in {images_path}")
        return raw.astype(np.uint8, copy=False).tobytes()
    blob = images_path.read_bytes()
    start = 16 + index * INPUT_SIZE
    end = start + INPUT_SIZE
    if end > len(blob):
        raise ValueError(f"Image index {index} exceeds {images_path}")
    return blob[start:end]


def write_single_idx_files(raw_image: bytes, label: int | None, out_dir: Path) -> tuple[Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / "single-image.idx3-ubyte"
    image_path.write_bytes(struct.pack(">IIII", 2051, 1, IMAGE_ROWS, IMAGE_COLS) + raw_image)

    if label is None:
        return image_path, None

    label_path = out_dir / "single-label.idx1-ubyte"
    label_path.write_bytes(struct.pack(">II", 2049, 1) + bytes([label]))
    return image_path, label_path


def write_single_np_files(raw_image: bytes, label: list[int] | None, out_dir: Path) -> tuple[Path, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    image_array = np.frombuffer(raw_image, dtype=np.uint8).reshape(IMAGE_ROWS, IMAGE_COLS)
    image_path = out_dir / "single-image.npy"
    np.save(image_path, image_array)

    if label is None:
        return image_path, None

    label_path = out_dir / "single-label.json"
    label_path.write_text(json.dumps({"labels": label}, indent=2) + "\n", encoding="utf-8")
    return image_path, label_path


def write_pgm_preview(raw_image: bytes, out_dir: Path) -> Path:
    preview_path = out_dir / "single-image.pgm"
    header = f"P5\n{IMAGE_COLS} {IMAGE_ROWS}\n255\n".encode("ascii")
    preview_path.write_bytes(header + raw_image)
    return preview_path


def write_query_input(normalized_image: list[float], out_path: Path) -> None:
    payload = {"input_data": [normalized_image]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def extract_mnist_sample(
    images_path: Path,
    labels_path: Path | None = None,
    index: int | None = None,
    out_dir: Path = Path("zk_inference/single_query"),
    input_json: Path = Path("zk_inference/out/input.json"),
    metadata_out: Path | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    selected_index = choose_random_index(images_path, seed=seed) if index is None else index
    normalized_image = load_image(images_path, selected_index)
    raw_image = read_raw_image_bytes(images_path, selected_index)

    label: int | list[int] | None = None
    if labels_path is not None:
        if is_multilabel_dataset():
            _, labels = read_npz_images_and_labels(labels_path)
            label = [int(value) for value in labels[selected_index].reshape(-1).tolist()]
        else:
            labels = read_idx_labels(labels_path, limit=selected_index + 1)
            label = int(labels[selected_index].item())

    if is_multilabel_dataset():
        image_path, label_path = write_single_np_files(raw_image, label if isinstance(label, list) else None, out_dir)
    else:
        image_path, label_path = write_single_idx_files(raw_image, label if isinstance(label, int) else None, out_dir)
    preview_path = write_pgm_preview(raw_image, out_dir)
    write_query_input(normalized_image, input_json)

    image_file_key = "single_image_npy" if is_multilabel_dataset() else "single_image_idx"
    label_file_key = "single_label_json" if is_multilabel_dataset() else "single_label_idx"
    metadata = {
        "source_images": metadata_path(images_path),
        "source_labels": metadata_path(labels_path) if labels_path is not None else None,
        "source_index": selected_index,
        "dataset": current_dataset_name(),
        "random_selection": index is None,
        "true_label": label,
        "files": {
            image_file_key: metadata_path(image_path),
            label_file_key: metadata_path(label_path) if label_path is not None else None,
            "preview_pgm": metadata_path(preview_path),
            "ezkl_input_json": metadata_path(input_json),
        },
    }

    if metadata_out is not None:
        metadata_out.parent.mkdir(parents=True, exist_ok=True)
        metadata_out.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        metadata["metadata_file"] = metadata_path(metadata_out)

    return metadata


def main() -> int:
    dataset = current_dataset_name()
    default_images = Path("data/chestmnist/test_data/test-data.npz") if dataset == "chestmnist" else Path("data/mnist/data/t10k-images.idx3-ubyte")
    default_labels = Path("data/chestmnist/test_data/test-data.npz") if dataset == "chestmnist" else Path("data/mnist/data/t10k-labels.idx1-ubyte")
    parser = argparse.ArgumentParser(description="Extract one dataset sample and create an EZKL input.json.")
    parser.add_argument("--images", type=Path, default=default_images)
    parser.add_argument("--labels", type=Path, default=default_labels)
    parser.add_argument("--index", type=int, default=None, help="Use a specific sample index instead of a random one")
    parser.add_argument("--seed", type=int, default=None, help="Optional seed for reproducible random selection")
    parser.add_argument("--out-dir", type=Path, default=Path("zk_inference/single_query"))
    parser.add_argument("--input-json", type=Path, default=Path("zk_inference/out/input.json"))
    parser.add_argument("--metadata-out", type=Path, default=Path("zk_inference/single_query/selection.json"))
    args = parser.parse_args()

    metadata = extract_mnist_sample(
        images_path=args.images,
        labels_path=args.labels,
        index=args.index,
        out_dir=args.out_dir,
        input_json=args.input_json,
        metadata_out=args.metadata_out,
        seed=args.seed,
    )

    print(f"Selected {dataset} test index: {metadata['source_index']}")
    print(f"Random selection: {metadata['random_selection']}")
    if metadata["true_label"] is not None:
        print(f"True label: {metadata['true_label']}")
    print(f"Wrote {args.input_json}")
    print(f"Wrote {args.metadata_out}")
    print(f"Input length: {len(load_image(args.images, metadata['source_index']))} (expected {INPUT_SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
