from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

from dfl.neural_network import cli

REPO_ROOT = Path(__file__).resolve().parents[3]
CHESTMNIST_BOOTSTRAP = REPO_ROOT / "data" / "initial_gm" / "chestmnist" / "aggregated.bin"
EXPECTED_BOOTSTRAP_SHA256 = "8dfe51ae6de4a5772927efa216cc8b0383ac6aac711cc69000230b4b464e54cd"


class TrainingOptimizationTests(unittest.TestCase):
    def test_chestmnist_bootstrap_is_reproducible_and_committed(self) -> None:
        environment = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "DATASET_NAME": "chestmnist",
            "DFL_MODEL_SEED": "42",
        }
        generated_sha256 = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "import hashlib; "
                    "from dfl.neural_network.cli import model_to_bytes, random_model; "
                    "print(hashlib.sha256(model_to_bytes(random_model())).hexdigest())"
                ),
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
        ).strip()
        self.assertEqual(generated_sha256, EXPECTED_BOOTSTRAP_SHA256)
        self.assertEqual(
            hashlib.sha256(CHESTMNIST_BOOTSTRAP.read_bytes()).hexdigest(),
            EXPECTED_BOOTSTRAP_SHA256,
        )
        self.assertEqual(CHESTMNIST_BOOTSTRAP.stat().st_size, 214_640)

    def test_task_wide_positive_weights_use_ratio_and_cap(self) -> None:
        weights = cli.multilabel_pos_weight_from_counts(
            100,
            [50, 25, 10, 5, 1, 20, 40, 50, 60, 70, 80, 90, 30, 2],
            cap=10,
        )
        self.assertAlmostEqual(float(weights[0]), 1.0)
        self.assertAlmostEqual(float(weights[1]), 3.0)
        self.assertAlmostEqual(float(weights[2]), 9.0)
        self.assertAlmostEqual(float(weights[3]), 10.0)
        self.assertAlmostEqual(float(weights[-1]), 10.0)

    def test_chestmnist_defaults_to_adamw(self) -> None:
        model = cli.FederatedCNN().float()
        with (
            patch.object(cli, "DATASET_NAME", "chestmnist"),
            patch.dict(
                os.environ,
                {
                    "DFL_TRAIN_OPTIMIZER": "",
                    "DFL_TRAIN_LEARNING_RATE": "",
                    "DFL_TRAIN_WEIGHT_DECAY": "",
                    "DFL_TRAIN_LR_SCHEDULE": "constant",
                },
            ),
        ):
            optimizer = cli.build_training_optimizer(model, round_id=3)
        self.assertIsInstance(optimizer, torch.optim.AdamW)
        self.assertAlmostEqual(float(optimizer.param_groups[0]["lr"]), 0.003)
        self.assertAlmostEqual(float(optimizer.param_groups[0]["weight_decay"]), 0.0001)

    def test_training_seed_binds_round_and_device(self) -> None:
        with patch.dict(os.environ, {"DFL_TRAIN_SEED": "42"}):
            baseline = cli.deterministic_training_seed(4, "worker-7")
            self.assertEqual(baseline, cli.deterministic_training_seed(4, "worker-7"))
            self.assertNotEqual(baseline, cli.deterministic_training_seed(5, "worker-7"))
            self.assertNotEqual(baseline, cli.deterministic_training_seed(4, "worker-8"))

    def test_late_cosine_schedule_starts_late_and_reaches_final_factor(self) -> None:
        with (
            patch.object(cli, "DATASET_NAME", "chestmnist"),
            patch.dict(
                os.environ,
                {
                    "ROUND": "50",
                    "DFL_TRAIN_LEARNING_RATE": "0.003",
                    "DFL_TRAIN_LR_SCHEDULE": "late_cosine",
                    "DFL_TRAIN_LR_DECAY_START_ROUND": "20",
                    "DFL_TRAIN_LR_FINAL_FACTOR": "0.25",
                },
            ),
        ):
            self.assertAlmostEqual(cli.training_learning_rate(20), 0.003)
            self.assertLess(cli.training_learning_rate(35), 0.003)
            self.assertAlmostEqual(cli.training_learning_rate(49), 0.00075)


if __name__ == "__main__":
    unittest.main()
