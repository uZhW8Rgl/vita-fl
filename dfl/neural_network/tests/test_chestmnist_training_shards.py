from __future__ import annotations

import random
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[3]
SHARD_DIR = REPO_ROOT / "data" / "chestmnist" / "training_data"
TRAINING_SAMPLE_COUNT = 78_468
WORKER_COUNT = 6
REQUIRED_FIELDS = {
    "images",
    "labels",
    "sample_indices",
    "device_signer_ids",
    "device_signatures",
    "radiologist_signer_ids",
    "radiologist_signatures",
    "provenance_version",
}


class ChestMnistTrainingShardTests(unittest.TestCase):
    def test_signed_shards_are_exact_disjoint_seed_42_partition(self) -> None:
        paths = sorted(
            SHARD_DIR.glob("train-data-*.npz"),
            key=lambda path: int(path.stem.removeprefix("train-data-")),
        )
        self.assertEqual(
            [path.name for path in paths],
            [f"train-data-{worker_id}.npz" for worker_id in range(WORKER_COUNT)],
        )

        actual_indices: list[int] = []
        actual_shards: list[list[int]] = []
        for worker_id, path in enumerate(paths):
            expected_size = TRAINING_SAMPLE_COUNT // WORKER_COUNT
            with np.load(path, allow_pickle=False) as bundle:
                self.assertEqual(set(bundle.files), REQUIRED_FIELDS)
                self.assertEqual(int(bundle["images"].shape[0]), expected_size)
                self.assertEqual(bundle["labels"].shape, (expected_size, 14))
                for field in REQUIRED_FIELDS - {"provenance_version"}:
                    self.assertEqual(
                        int(bundle[field].shape[0]),
                        expected_size,
                        msg=f"{path.name}:{field}",
                    )
                shard_indices = [int(value) for value in bundle["sample_indices"]]
            actual_shards.append(shard_indices)
            actual_indices.extend(shard_indices)

        self.assertEqual(len(actual_indices), TRAINING_SAMPLE_COUNT)
        self.assertEqual(len(set(actual_indices)), TRAINING_SAMPLE_COUNT)
        self.assertEqual(sorted(actual_indices), list(range(TRAINING_SAMPLE_COUNT)))

        expected_indices = list(range(TRAINING_SAMPLE_COUNT))
        random.Random(42).shuffle(expected_indices)
        offset = 0
        for worker_id, actual_shard in enumerate(actual_shards):
            expected_size = TRAINING_SAMPLE_COUNT // WORKER_COUNT
            self.assertEqual(actual_shard, expected_indices[offset : offset + expected_size])
            offset += expected_size


if __name__ == "__main__":
    unittest.main()
