from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dfl.neural_network.dicom_provenance import (  # noqa: E402
    PROVENANCE_VERSION,
    load_rsa_private_key,
    sign_sample,
)


def load_split(bundle: np.lib.npyio.NpzFile, images_key: str, labels_key: str) -> tuple[np.ndarray, np.ndarray]:
    if images_key not in bundle or labels_key not in bundle:
        raise ValueError(f"Missing {images_key!r} or {labels_key!r} in bundle")
    images = bundle[images_key]
    labels = bundle[labels_key]
    if images.shape[0] != labels.shape[0]:
        raise ValueError(f"Mismatched split sizes for {images_key} and {labels_key}")
    return images, labels


def write_npz(
    path: Path,
    images: np.ndarray,
    labels: np.ndarray,
    sample_indices: np.ndarray,
    device_ids: np.ndarray,
    device_signatures: np.ndarray,
    radiologist_ids: np.ndarray,
    radiologist_signatures: np.ndarray,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        images=images,
        labels=labels,
        sample_indices=sample_indices,
        device_signer_ids=device_ids,
        device_signatures=device_signatures,
        radiologist_signer_ids=radiologist_ids,
        radiologist_signatures=radiologist_signatures,
        provenance_version=np.bytes_(PROVENANCE_VERSION),
    )


def sign_shard(
    images: np.ndarray,
    labels: np.ndarray,
    sample_indices: np.ndarray,
    device_private_keys: list,
    radiologist_private_key,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    device_ids: list[bytes] = []
    device_signatures: list[bytes] = []
    radiologist_ids: list[bytes] = []
    radiologist_signatures: list[bytes] = []
    for image, label, source_index in zip(images, labels, sample_indices, strict=True):
        sample_index = int(source_index)
        device_index = sample_index % len(device_private_keys)
        device_id = f"XRAY_DEVICE_{device_index}"
        radiologist_id = "RADIOLOGIST_0"
        image_signature, label_signature = sign_sample(
            image,
            label,
            sample_index,
            device_id,
            device_private_keys[device_index],
            radiologist_id,
            radiologist_private_key,
        )
        device_ids.append(device_id.encode("ascii"))
        device_signatures.append(image_signature)
        radiologist_ids.append(radiologist_id.encode("ascii"))
        radiologist_signatures.append(label_signature)
    return (
        np.asarray(device_ids, dtype="S32"),
        np.asarray([np.frombuffer(signature, dtype=np.uint8) for signature in device_signatures]),
        np.asarray(radiologist_ids, dtype="S32"),
        np.asarray([np.frombuffer(signature, dtype=np.uint8) for signature in radiologist_signatures]),
    )


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
        "--workers",
        type=int,
        default=500,
        help="Number of worker splits to create",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Shuffle seed for reproducible IID partitioning",
    )
    parser.add_argument(
        "--signer-private-dir",
        type=Path,
        default=Path("data/chestmnist/provenance/private"),
        help="Directory containing the synthetic DICOM signer fixture private keys",
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

    device_private_keys = [
        load_rsa_private_key(args.signer_private_dir / f"xray-device-{index}-private.pem") for index in range(5)
    ]
    radiologist_private_key = load_rsa_private_key(args.signer_private_dir / "radiologist-0-private.pem")

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
        shard_images = train_images[shard_indices]
        shard_labels = train_labels[shard_indices]
        device_ids, device_signatures, radiologist_ids, radiologist_signatures = sign_shard(
            shard_images,
            shard_labels,
            shard_indices,
            device_private_keys,
            radiologist_private_key,
        )
        write_npz(
            args.train_out_dir / f"train-data-{worker_id}.npz",
            shard_images,
            shard_labels,
            shard_indices,
            device_ids,
            device_signatures,
            radiologist_ids,
            radiologist_signatures,
        )
        print(f"worker {worker_id}: {size} samples")

    args.test_out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.test_out, images=test_images, labels=test_labels)
    print(f"wrote test split: {args.test_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
