from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

os.environ["DATASET_NAME"] = "chestmnist"

from neural_network.cli import (  # noqa: E402
    FederatedCNN,
    MODEL_TRANSFER_FILENAME,
    _append_csv_rows,
    _binary_f1,
    _binary_average_precision,
    _evaluate_multilabel,
    aggregate,
    best_f1_threshold,
    build_training_criterion,
    build_training_optimizer,
    calibrate_multilabel_thresholds,
    deterministic_training_seed,
    model_byte_size,
    model_to_bytes,
    multilabel_pos_weight_from_counts,
    parse_model_transfer_filename,
    random_model,
    read_model_bin,
    read_npz_images_and_labels,
    training_learning_rate,
    train_model,
    write_model_bin,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class FederatedTrainingTests(unittest.TestCase):
    def test_chestmnist_model_shape_and_activation_path(self) -> None:
        model = FederatedCNN().float()
        outputs = model(torch.zeros(2, 784))
        self.assertEqual(tuple(outputs.shape), (2, 14))
        self.assertEqual(model.conv1.out_channels, 8)
        self.assertEqual(model.conv2.out_channels, 16)
        self.assertEqual(model.fc1.in_features, 16 * 7 * 7)
        self.assertEqual(sum(parameter.numel() for parameter in model.parameters()), 26_830)
        self.assertEqual(model_byte_size(), 214_640)

    def test_bootstrap_initialization_is_reproducible_and_fan_in_aware(self) -> None:
        first = random_model(seed=42)
        second = random_model(seed=42)
        third = random_model(seed=43)
        first_state = first.state_dict()
        second_state = second.state_dict()
        third_state = third.state_dict()

        for name in first_state:
            self.assertTrue(torch.equal(first_state[name], second_state[name]))
        self.assertTrue(any(not torch.equal(first_state[name], third_state[name]) for name in first_state))
        self.assertLess(float(first.fc1.weight.detach().std()), 0.1)

    def test_committed_bootstrap_matches_seed_42(self) -> None:
        bootstrap = REPO_ROOT / "data/initial_gm/chestmnist/aggregated.bin"
        self.assertEqual(bootstrap.read_bytes(), model_to_bytes(random_model(seed=42)))

    def test_binary_layout_round_trip_matches_current_dataset_layout(self) -> None:
        model = random_model(seed=42)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.bin"
            write_model_bin(model, path)
            restored = read_model_bin(path)
            self.assertEqual(path.stat().st_size, model_byte_size())
            for name, value in model.state_dict().items():
                self.assertTrue(torch.equal(value, restored.state_dict()[name]))

    def test_task_wide_positive_weights_are_ratio_based_and_capped(self) -> None:
        counts = [7996, 1950, 9261, 13914, 3988, 4375, 978, 3705, 3263, 1690, 1799, 1158, 2279, 144]
        weights = multilabel_pos_weight_from_counts(78468, counts, cap=20.0)
        self.assertAlmostEqual(float(weights[0]), (78468 - 7996) / 7996, places=5)
        self.assertEqual(float(weights[1]), 20.0)
        self.assertEqual(float(weights[-1]), 20.0)

    def test_training_metadata_matches_the_official_training_split(self) -> None:
        metadata_path = REPO_ROOT / "data/chestmnist/training-metadata.json"
        source_path = REPO_ROOT / "data/chestmnist/chestmnist.npz"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        with np.load(source_path) as bundle:
            labels = bundle["train_labels"].reshape(-1, 14)
        self.assertEqual(int(labels.shape[0]), metadata["sample_count"])
        self.assertEqual(
            labels.astype(np.int64).sum(axis=0).tolist(),
            metadata["positive_counts"],
        )

    def test_twenty_five_worker_shards_cover_the_complete_training_split(self) -> None:
        shard_dir = REPO_ROOT / "data/chestmnist/training_data"
        shard_paths = [shard_dir / f"train-data-{index}.npz" for index in range(25)]
        self.assertTrue(all(path.exists() for path in shard_paths))

        sample_counts: list[int] = []
        positive_counts = np.zeros(14, dtype=np.int64)
        for path in shard_paths:
            with np.load(path) as bundle:
                labels = bundle["labels"].reshape(-1, 14).astype(np.int64)
            sample_counts.append(int(labels.shape[0]))
            positive_counts += labels.sum(axis=0)

        metadata = json.loads((REPO_ROOT / "data/chestmnist/training-metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(sample_counts), metadata["sample_count"])
        self.assertLessEqual(max(sample_counts) - min(sample_counts), 1)
        self.assertEqual(positive_counts.tolist(), metadata["positive_counts"])

    def test_non_finite_parameters_are_not_serialized(self) -> None:
        model = random_model(seed=42)
        with torch.no_grad():
            model.fc2.bias[0] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            model_to_bytes(model)

    def test_fedavg_accepts_24_updates_for_a_25_participant_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            received = root / "received_models"
            results = root / "results"
            received.mkdir()
            results.mkdir()

            base_model = random_model(seed=42)
            for index in range(24):
                local_model = random_model(seed=42)
                with torch.no_grad():
                    local_model.fc2.bias.add_(index / 1000.0)
                write_model_bin(local_model, received / f"worker-{index}.bin")

            with (
                patch("neural_network.cli.received_models_dir", return_value=received),
                patch("neural_network.cli.results_dir", return_value=results),
                patch("neural_network.cli.sign_file"),
                patch("neural_network.cli.run_test", return_value={}),
            ):
                payload = aggregate(24, participant_count=25, expected_models=24)

            aggregated = read_model_bin(Path(payload["model_path"]))
            expected_bias = base_model.fc2.bias.detach() + (sum(range(24)) / 24 / 1000.0)
            self.assertTrue(torch.allclose(aggregated.fc2.bias, expected_bias))
            self.assertEqual(Path(payload["model_path"]).stat().st_size, model_byte_size())

    def test_training_seed_is_stable_per_worker_and_round(self) -> None:
        with patch.dict(os.environ, {"DFL_TRAIN_SEED": "42"}):
            self.assertEqual(deterministic_training_seed(3, "7"), deterministic_training_seed(3, "7"))
            self.assertNotEqual(deterministic_training_seed(3, "7"), deterministic_training_seed(3, "8"))
            self.assertNotEqual(deterministic_training_seed(3, "7"), deterministic_training_seed(4, "7"))
            self.assertEqual(deterministic_training_seed(3, 0), deterministic_training_seed(3, "0"))

    def test_chestmnist_optimizer_defaults_to_adamw(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DFL_TRAIN_OPTIMIZER": "",
                "DFL_TRAIN_LEARNING_RATE": "",
                "DFL_TRAIN_WEIGHT_DECAY": "",
            },
        ):
            optimizer = build_training_optimizer(FederatedCNN().float())
        self.assertIsInstance(optimizer, torch.optim.AdamW)
        self.assertAlmostEqual(float(optimizer.param_groups[0]["lr"]), 0.003)
        self.assertAlmostEqual(float(optimizer.param_groups[0]["weight_decay"]), 0.0001)

    def test_late_cosine_learning_rate_only_decays_in_final_rounds(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DFL_TRAIN_LEARNING_RATE": "0.003",
                "DFL_TRAIN_LR_SCHEDULE": "late_cosine",
                "DFL_TRAIN_LR_DECAY_START_ROUND": "20",
                "DFL_TRAIN_LR_FINAL_FACTOR": "0.25",
                "ROUND": "25",
            },
        ):
            rates = [training_learning_rate(round_id) for round_id in (19, 20, 21, 22, 23, 24)]
        self.assertAlmostEqual(rates[0], 0.003)
        self.assertAlmostEqual(rates[1], 0.003)
        self.assertAlmostEqual(rates[2], 0.002670495128834866)
        self.assertAlmostEqual(rates[3], 0.001875)
        self.assertAlmostEqual(rates[-1], 0.00075)
        self.assertTrue(all(current >= following for current, following in zip(rates, rates[1:])))

    def test_f1_for_no_positive_predictions_is_zero(self) -> None:
        self.assertEqual(_binary_f1(0, 0, 10), 0.0)
        self.assertEqual(_binary_f1(0, 0, 0), 0.0)
        self.assertAlmostEqual(_binary_f1(3, 1, 2), 6 / 9)

    def test_average_precision_and_validation_f1_threshold_are_deterministic(self) -> None:
        scores = np.asarray([0.9, 0.8, 0.7, 0.1], dtype=np.float64)
        labels = np.asarray([1, 0, 1, 0], dtype=np.int64)
        self.assertAlmostEqual(_binary_average_precision(scores, labels), (1.0 + 2 / 3) / 2)
        self.assertAlmostEqual(best_f1_threshold(scores, labels), 0.7)

    def test_multilabel_thresholds_are_calibrated_per_label(self) -> None:
        probabilities = np.asarray(
            [
                [0.9, 0.4],
                [0.8, 0.3],
                [0.7, 0.2],
                [0.1, 0.1],
            ],
            dtype=np.float64,
        )
        labels = np.asarray(
            [
                [1, 1],
                [0, 0],
                [1, 1],
                [0, 0],
            ],
            dtype=np.int64,
        )
        thresholds = calibrate_multilabel_thresholds(probabilities, labels)
        np.testing.assert_allclose(thresholds, np.asarray([0.7, 0.2]))

    def test_validation_f1_threshold_can_cap_predicted_positive_prevalence(self) -> None:
        scores = np.asarray([0.9, 0.8, 0.7, 0.6, 0.5, 0.4], dtype=np.float64)
        labels = np.asarray([0, 0, 0, 0, 0, 1], dtype=np.int64)
        self.assertAlmostEqual(best_f1_threshold(scores, labels), 0.4)
        capped = best_f1_threshold(
            scores,
            labels,
            max_predicted_positive_multiplier=2.0,
        )
        self.assertAlmostEqual(capped, 0.8)
        self.assertLessEqual(int((scores >= capped).sum()), 2 * int(labels.sum()))

    def test_calibrated_f1_is_reported_separately_from_fixed_point_five(self) -> None:
        probabilities = torch.full((2, 14), 0.1, dtype=torch.float32)
        labels = torch.zeros((2, 14), dtype=torch.float32)
        probabilities[0, 0] = 0.4
        labels[0, 0] = 1
        logits = torch.logit(probabilities)
        thresholds = np.full(14, 0.5, dtype=np.float64)
        thresholds[0] = 0.4
        summary, label_rows, _ = _evaluate_multilabel(logits, labels, {}, thresholds)
        self.assertGreater(summary["macro_f1"], summary["macro_f1_at_0_5"])
        self.assertEqual(label_rows[0]["decision_threshold"], 0.4)
        self.assertEqual(summary["threshold_source"], "validation_f1")

    def test_round_tagged_transfer_filename_is_strict(self) -> None:
        address = "0x" + "ab" * 20
        match = MODEL_TRANSFER_FILENAME.fullmatch(f"wb_client_{address}_round_7.enc")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("device"), address)
        self.assertEqual(match.group("round"), "7")
        self.assertIsNone(MODEL_TRANSFER_FILENAME.fullmatch(f"wb_client_{address}_round_07.enc"))
        self.assertIsNone(MODEL_TRANSFER_FILENAME.fullmatch("wb_client_../../escape_round_7.enc"))
        self.assertEqual(
            parse_model_transfer_filename(f"wb_client_{address.upper().replace('0X', '0x')}_round_7.enc"),
            (address, 7),
        )
        with self.assertRaisesRegex(ValueError, "expected round 8"):
            parse_model_transfer_filename(
                f"wb_client_{address}_round_7.enc",
                expected_round=8,
            )

    def test_metric_csv_schema_is_migrated_before_appending_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.csv"
            path.write_text("round,macro_f1\n1,0.1\n", encoding="utf-8")
            _append_csv_rows(
                path,
                ["round", "macro_f1", "macro_auprc"],
                [{"round": 2, "macro_f1": 0.2, "macro_auprc": 0.3}],
            )
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0], {"round": "1", "macro_f1": "0.1", "macro_auprc": ""})
            self.assertEqual(rows[1], {"round": "2", "macro_f1": "0.2", "macro_auprc": "0.3"})

    def test_torch_worker_thread_limits_apply_in_a_fresh_process(self) -> None:
        env = dict(os.environ)
        env.update({
            "DATASET_NAME": "chestmnist",
            "DFL_TORCH_NUM_THREADS": "1",
            "DFL_TORCH_INTEROP_THREADS": "1",
        })
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import torch; import neural_network.cli; "
                    "assert torch.get_num_threads() == 1; "
                    "assert torch.get_num_interop_threads() == 1"
                ),
            ],
            cwd=PYTHON_PACKAGE_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_one_worker_epoch_updates_the_bootstrap_model(self) -> None:
        bootstrap = REPO_ROOT / "data/initial_gm/chestmnist/aggregated.bin"
        shard = REPO_ROOT / "data/chestmnist/training_data/train-data-0.npz"
        metadata = REPO_ROOT / "data/chestmnist/training-metadata.json"
        images, labels = read_npz_images_and_labels(shard)

        def run_once() -> tuple[bytes, float]:
            with tempfile.TemporaryDirectory() as directory:
                node_dir = Path(directory)
                worker_data = node_dir / "data"
                worker_data.mkdir()
                shutil.copyfile(bootstrap, worker_data / "gm.bin")
                shutil.copyfile(shard, worker_data / "train-data.npz")
                with (
                    patch.dict(
                        os.environ,
                        {
                            "THESIS_NODE_SERVER_DIR": str(node_dir),
                            "CHESTMNIST_TRAINING_METADATA_PATH": str(metadata),
                            "DFL_TRAIN_OPTIMIZER": "adamw",
                            "DFL_TRAIN_LEARNING_RATE": "0.003",
                            "DFL_TRAIN_WEIGHT_DECAY": "0.0001",
                            "DFL_POS_WEIGHT_CAP": "20",
                            "DFL_TRAIN_SEED": "42",
                        },
                    ),
                    redirect_stdout(StringIO()),
                ):
                    train_model(1, "", round_id=1, device_id="0")

                local_model = worker_data / "lm.bin"
                model = read_model_bin(local_model).float().eval()
                criterion = build_training_criterion(dtype=torch.float32)
                with torch.no_grad():
                    loss = float(criterion(model(images.float()), labels.reshape(-1, 14).float()).item())
                self.assertTrue(torch.isfinite(torch.tensor(loss)).item())
                self.assertEqual(local_model.stat().st_size, model_byte_size())
                return local_model.read_bytes(), loss

        initial_model = read_model_bin(bootstrap).float().eval()
        initial_criterion = build_training_criterion(dtype=torch.float32)
        with torch.no_grad():
            initial_loss = float(
                initial_criterion(initial_model(images.float()), labels.reshape(-1, 14).float()).item()
            )
        first_bytes, first_loss = run_once()
        second_bytes, second_loss = run_once()
        self.assertNotEqual(first_bytes, bootstrap.read_bytes())
        self.assertEqual(first_bytes, second_bytes)
        self.assertAlmostEqual(first_loss, second_loss)
        self.assertLess(first_loss, initial_loss)

    def test_mnist_model_path_remains_available_in_a_fresh_process(self) -> None:
        env = dict(os.environ)
        env["DATASET_NAME"] = "mnist"
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import torch; "
                    "from neural_network.cli import FederatedCNN, OUTPUT_SIZE, model_byte_size; "
                    "assert OUTPUT_SIZE == 10; "
                    "model = FederatedCNN(); "
                    "assert tuple(model(torch.zeros(2, 784)).shape) == (2, 10); "
                    "assert model.conv1.out_channels == 8; "
                    "assert model.conv2.out_channels == 16; "
                    "assert model_byte_size() == 213584"
                ),
            ],
            cwd=PYTHON_PACKAGE_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
