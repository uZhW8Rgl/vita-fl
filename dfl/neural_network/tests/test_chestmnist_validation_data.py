from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dfl.neural_network.dicom_provenance import (
    VALIDATION_SPLIT_ID,
    validation_semantic_sha256,
    verify_training_provenance,
)
from dfl.neural_network.tests.test_dicom_provenance import signer_snapshot
from scripts.generate_chestmnist_training_splits import (
    _image_identity,
    select_disjoint_validation_indices,
)
from scripts.generate_chestmnist_training_splits import main as generate_splits

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_BUNDLE = REPO_ROOT / "data" / "chestmnist" / "chestmnist.npz"
VALIDATION_ARTIFACT = REPO_ROOT / "data" / "chestmnist" / "validation_data" / "validation-data.npz"
EXPECTED_VALIDATION_SEMANTIC_SHA256 = "e4457c09ceeb203858e9b74232a4aa5b8852c623d85a63742a8751405d189d63"
EXPECTED_VALIDATION_FILE_SHA256 = "9125cc0622896e8a8f0b4ee1d943b8bea4dda84b017324346475d3eebed0d9b4"


def _scalar_text(value: np.ndarray) -> str:
    return bytes(value.item()).rstrip(b"\x00").decode("ascii")


def _tiny_source(path: Path) -> None:
    train_images = np.stack(
        (
            np.zeros((28, 28), dtype=np.uint8),
            np.ones((28, 28), dtype=np.uint8),
        )
    )
    validation_images = np.stack(
        (
            np.ones((28, 28), dtype=np.uint8),
            np.full((28, 28), 2, dtype=np.uint8),
            np.full((28, 28), 2, dtype=np.uint8),
            np.full((28, 28), 3, dtype=np.uint8),
        )
    )
    train_labels = np.zeros((2, 14), dtype=np.uint8)
    validation_labels = np.zeros((4, 14), dtype=np.uint8)
    validation_labels[2, 3] = 1
    test_images = np.full((1, 28, 28), 4, dtype=np.uint8)
    test_labels = np.zeros((1, 14), dtype=np.uint8)
    np.savez_compressed(
        path,
        train_images=train_images,
        train_labels=train_labels,
        val_images=validation_images,
        val_labels=validation_labels,
        test_images=test_images,
        test_labels=test_labels,
    )


def _write_private_signer_fixtures(path: Path) -> None:
    path.mkdir(parents=True)
    for file_stem in [*(f"xray-device-{index}" for index in range(5)), "radiologist-0"]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (path / f"{file_stem}-private.pem").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


class ChestMnistValidationDataTests(unittest.TestCase):
    def test_filter_is_image_disjoint_and_retains_first_validation_occurrence(self) -> None:
        train_images = np.stack(
            (
                np.zeros((28, 28), dtype=np.uint8),
                np.ones((28, 28), dtype=np.uint8),
            )
        )
        validation_images = np.stack(
            (
                np.ones((28, 28), dtype=np.uint8),
                np.full((28, 28), 2, dtype=np.uint8),
                np.full((28, 28), 2, dtype=np.uint8),
                np.full((28, 28), 3, dtype=np.uint8),
            )
        )

        selected, stats = select_disjoint_validation_indices(train_images, validation_images)

        np.testing.assert_array_equal(selected, np.asarray([1, 3], dtype="<i8"))
        self.assertEqual(stats["removed_train_overlaps"], 1)
        self.assertEqual(stats["removed_internal_duplicates"], 1)

    def test_validation_only_is_reproducible_and_does_not_write_train_or_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.npz"
            first = root / "first.npz"
            second = root / "second.npz"
            train_out = root / "training"
            test_out = root / "test.npz"
            signer_private_dir = root / "signers"
            _tiny_source(source)
            _write_private_signer_fixtures(signer_private_dir)

            common = [
                "--source",
                str(source),
                "--validation-only",
                "--train-out-dir",
                str(train_out),
                "--test-out",
                str(test_out),
                "--signer-private-dir",
                str(signer_private_dir),
            ]
            self.assertEqual(generate_splits([*common, "--validation-out", str(first)]), 0)
            self.assertEqual(generate_splits([*common, "--validation-out", str(second)]), 0)

            self.assertFalse(train_out.exists())
            self.assertFalse(test_out.exists())
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with np.load(first, allow_pickle=False) as bundle:
                self.assertEqual(_scalar_text(bundle["dataset_split"]), VALIDATION_SPLIT_ID)
                np.testing.assert_array_equal(bundle["sample_indices"], np.asarray([3, 5], dtype="<i8"))
                self.assertEqual(_scalar_text(bundle["semantic_sha256"]), validation_semantic_sha256(bundle))

    def test_split_identifier_is_cryptographically_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tampered = root / "tampered.npz"
            with np.load(VALIDATION_ARTIFACT, allow_pickle=False) as bundle:
                values = {name: bundle[name].copy() for name in bundle.files}
            values["dataset_split"] = np.bytes_("CHESTMNIST-TEST-V1")
            np.savez_compressed(tampered, **values)

            with self.assertRaisesRegex(ValueError, "provenance verification failed"):
                verify_training_provenance(tampered, signer_snapshot())

    def test_checked_in_validation_artifact_matches_source_and_hash(self) -> None:
        with np.load(SOURCE_BUNDLE, allow_pickle=False) as source:
            train_images = source["train_images"]
            validation_images = source["val_images"]
            validation_labels = source["val_labels"]
            selected, stats = select_disjoint_validation_indices(train_images, validation_images)

            with np.load(VALIDATION_ARTIFACT, allow_pickle=False) as artifact:
                self.assertEqual(_scalar_text(artifact["dataset_split"]), VALIDATION_SPLIT_ID)
                self.assertEqual(_scalar_text(artifact["semantic_sha256"]), EXPECTED_VALIDATION_SEMANTIC_SHA256)
                self.assertEqual(validation_semantic_sha256(artifact), EXPECTED_VALIDATION_SEMANTIC_SHA256)
                self.assertEqual(validation_semantic_sha256(VALIDATION_ARTIFACT), EXPECTED_VALIDATION_SEMANTIC_SHA256)
                self.assertEqual(int(artifact["images"].shape[0]), 11_212)
                self.assertEqual(stats["removed_train_overlaps"], 2)
                self.assertEqual(stats["removed_internal_duplicates"], 5)
                np.testing.assert_array_equal(
                    artifact["sample_indices"],
                    np.asarray(int(train_images.shape[0]) + selected, dtype="<i8"),
                )
                np.testing.assert_array_equal(artifact["images"], validation_images[selected])
                np.testing.assert_array_equal(artifact["labels"], validation_labels[selected])

                training_identities = {_image_identity(image) for image in train_images}
                validation_identities = [_image_identity(image) for image in artifact["images"]]
                self.assertEqual(len(validation_identities), len(set(validation_identities)))
                self.assertTrue(training_identities.isdisjoint(validation_identities))

        verification = verify_training_provenance(VALIDATION_ARTIFACT, signer_snapshot())
        self.assertEqual(verification["verified_samples"], 11_212)
        self.assertEqual(verification["dataset_split"], VALIDATION_SPLIT_ID)
        self.assertEqual(
            hashlib.sha256(VALIDATION_ARTIFACT.read_bytes()).hexdigest(),
            EXPECTED_VALIDATION_FILE_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
