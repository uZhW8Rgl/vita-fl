from __future__ import annotations

import argparse
import hashlib
import json
import random
import struct
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dfl.neural_network.dicom_provenance import (  # noqa: E402
    PROVENANCE_VERSION,
    VALIDATION_SPLIT_ID,
    load_rsa_private_key,
    sign_sample,
    validation_semantic_sha256,
)


def load_split(bundle: np.lib.npyio.NpzFile, images_key: str, labels_key: str) -> tuple[np.ndarray, np.ndarray]:
    if images_key not in bundle or labels_key not in bundle:
        raise ValueError(f"Missing {images_key!r} or {labels_key!r} in bundle")
    images = bundle[images_key]
    labels = bundle[labels_key]
    if images.shape[0] != labels.shape[0]:
        raise ValueError(f"Mismatched split sizes for {images_key} and {labels_key}")
    return images, labels


def _image_identity(image: np.ndarray) -> bytes:
    array = np.ascontiguousarray(image)
    digest = hashlib.sha256(b"VITA-FL:CHESTMNIST:IMAGE-IDENTITY:V1\x00")
    digest.update(array.dtype.str.encode("ascii"))
    digest.update(struct.pack("<H", array.ndim))
    for dimension in array.shape:
        digest.update(struct.pack("<Q", int(dimension)))
    digest.update(array.tobytes(order="C"))
    return digest.digest()


def select_disjoint_validation_indices(
    train_images: np.ndarray,
    validation_images: np.ndarray,
) -> tuple[np.ndarray, dict[str, int]]:
    """Keep the first validation occurrence of images never present in training."""

    training_identities = {_image_identity(image) for image in train_images}
    validation_identities: set[bytes] = set()
    selected: list[int] = []
    train_overlaps = 0
    internal_duplicates = 0
    for source_index, image in enumerate(validation_images):
        identity = _image_identity(image)
        if identity in training_identities:
            train_overlaps += 1
            continue
        if identity in validation_identities:
            internal_duplicates += 1
            continue
        validation_identities.add(identity)
        selected.append(source_index)
    return np.asarray(selected, dtype="<i8"), {
        "source_samples": int(validation_images.shape[0]),
        "selected_samples": len(selected),
        "removed_train_overlaps": train_overlaps,
        "removed_internal_duplicates": internal_duplicates,
    }


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


def write_training_metadata(path: Path, labels: np.ndarray) -> None:
    flattened = labels.reshape(labels.shape[0], -1)
    if flattened.shape[1] != 14:
        raise ValueError(f"Expected 14 ChestMNIST labels, got {flattened.shape[1]}")
    if not np.isfinite(flattened).all() or not np.isin(flattened, (0, 1)).all():
        raise ValueError("ChestMNIST labels must be finite binary values")
    positive_counts = flattened.astype(np.int64).sum(axis=0)
    sample_count = int(flattened.shape[0])
    metadata = {
        "schema_version": 1,
        "dataset": "chestmnist",
        "source_split": "train",
        "sample_count": sample_count,
        "label_count": int(flattened.shape[1]),
        "positive_counts": [int(value) for value in positive_counts],
        "negative_counts": [int(sample_count - value) for value in positive_counts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def write_validation_npz(
    path: Path,
    images: np.ndarray,
    labels: np.ndarray,
    sample_indices: np.ndarray,
    device_ids: np.ndarray,
    device_signatures: np.ndarray,
    radiologist_ids: np.ndarray,
    radiologist_signatures: np.ndarray,
) -> tuple[str, str]:
    values: dict[str, Any] = {
        "images": np.ascontiguousarray(images, dtype=np.uint8),
        "labels": np.ascontiguousarray(labels, dtype=np.uint8),
        "sample_indices": np.ascontiguousarray(sample_indices, dtype="<i8"),
        "device_signer_ids": np.ascontiguousarray(device_ids),
        "device_signatures": np.ascontiguousarray(device_signatures, dtype=np.uint8),
        "radiologist_signer_ids": np.ascontiguousarray(radiologist_ids),
        "radiologist_signatures": np.ascontiguousarray(radiologist_signatures, dtype=np.uint8),
        "provenance_version": np.bytes_(PROVENANCE_VERSION),
        "dataset_split": np.bytes_(VALIDATION_SPLIT_ID),
    }
    semantic_sha256 = validation_semantic_sha256(values)
    values["semantic_sha256"] = np.bytes_(semantic_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **values)
    file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return semantic_sha256, file_sha256


def sign_shard(
    images: np.ndarray,
    labels: np.ndarray,
    sample_indices: np.ndarray,
    device_private_keys: list,
    radiologist_private_key,
    *,
    dataset_split: str | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    device_ids: list[bytes] = []
    device_signatures: list[bytes] = []
    radiologist_ids: list[bytes] = []
    radiologist_signatures: list[bytes] = []
    if not (int(images.shape[0]) == int(labels.shape[0]) == int(sample_indices.shape[0])):
        raise ValueError("cannot sign arrays with inconsistent sample counts")
    for image, label, source_index in zip(images, labels, sample_indices):
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
            dataset_split=dataset_split,
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


def generate_validation_split(
    *,
    train_images: np.ndarray,
    validation_images: np.ndarray,
    validation_labels: np.ndarray,
    validation_out: Path,
    device_private_keys: list,
    radiologist_private_key: Any,
) -> dict[str, int | str]:
    selected_indices, stats = select_disjoint_validation_indices(train_images, validation_images)
    if selected_indices.size == 0:
        raise ValueError("strict validation filtering removed every source sample")
    selected_images = validation_images[selected_indices]
    selected_labels = validation_labels[selected_indices]
    global_sample_indices = np.asarray(
        int(train_images.shape[0]) + selected_indices,
        dtype="<i8",
    )
    device_ids, device_signatures, radiologist_ids, radiologist_signatures = sign_shard(
        selected_images,
        selected_labels,
        global_sample_indices,
        device_private_keys,
        radiologist_private_key,
        dataset_split=VALIDATION_SPLIT_ID,
    )
    semantic_sha256, file_sha256 = write_validation_npz(
        validation_out,
        selected_images,
        selected_labels,
        global_sample_indices,
        device_ids,
        device_signatures,
        radiologist_ids,
        radiologist_signatures,
    )
    return {
        **stats,
        "split_id": VALIDATION_SPLIT_ID,
        "first_global_sample_index": int(global_sample_indices[0]),
        "last_global_sample_index": int(global_sample_indices[-1]),
        "semantic_sha256": semantic_sha256,
        "file_sha256": file_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
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
        "--validation-out",
        type=Path,
        default=Path("data/chestmnist/validation_data/validation-data.npz"),
        help="Destination for the signed, strictly disjoint validation split",
    )
    parser.add_argument(
        "--metadata-out",
        type=Path,
        default=Path("data/chestmnist/training-metadata.json"),
        help="Destination for task-wide training-label statistics",
    )
    parser.add_argument(
        "--validation-only",
        action="store_true",
        help="Generate only the signed validation artifact; do not rewrite training shards or test data",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
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
    args = parser.parse_args(argv)

    if args.workers < 1:
        raise ValueError("--workers must be at least 1")

    if not args.source.exists():
        raise FileNotFoundError(
            f"ChestMNIST source file not found: {args.source}\n"
            "Download the official bundle to data/chestmnist/chestmnist.npz first, for example with:\n"
            "  .venv/bin/python scripts/download_chestmnist.py"
        )

    with np.load(args.source) as bundle:
        train_images, train_labels = load_split(bundle, "train_images", "train_labels")
        validation_images, validation_labels = load_split(bundle, "val_images", "val_labels")
        if args.validation_only:
            test_images = test_labels = None
        else:
            test_images, test_labels = load_split(bundle, "test_images", "test_labels")

    device_private_keys = [
        load_rsa_private_key(args.signer_private_dir / f"xray-device-{index}-private.pem") for index in range(5)
    ]
    radiologist_private_key = load_rsa_private_key(args.signer_private_dir / "radiologist-0-private.pem")

    if not args.validation_only:
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

        write_training_metadata(args.metadata_out, train_labels)
        print(f"wrote training metadata: {args.metadata_out}")

        assert test_images is not None and test_labels is not None
        args.test_out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.test_out, images=test_images, labels=test_labels)
        print(f"wrote test split: {args.test_out}")

    validation_result = generate_validation_split(
        train_images=train_images,
        validation_images=validation_images,
        validation_labels=validation_labels,
        validation_out=args.validation_out,
        device_private_keys=device_private_keys,
        radiologist_private_key=radiologist_private_key,
    )
    print(
        f"wrote signed validation split: {args.validation_out} "
        f"(selected={validation_result['selected_samples']}, "
        f"train-overlaps-removed={validation_result['removed_train_overlaps']}, "
        f"internal-duplicates-removed={validation_result['removed_internal_duplicates']})"
    )
    print(f"validation semantic SHA-256: {validation_result['semantic_sha256']}")
    print(f"validation file SHA-256: {validation_result['file_sha256']}")

    if not args.validation_only:
        stale_shards = []
        for path in args.train_out_dir.glob("train-data-*.npz"):
            worker_id = path.stem.removeprefix("train-data-")
            if worker_id.isdigit() and int(worker_id) >= args.workers:
                stale_shards.append(path)
        for path in sorted(stale_shards):
            path.unlink()
        if stale_shards:
            print(f"removed {len(stale_shards)} stale worker shards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
