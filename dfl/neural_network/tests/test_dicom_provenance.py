from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from dfl.neural_network.dicom_provenance import verify_training_provenance

REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLIC_SIGNER_DIR = REPO_ROOT / "smart_contracts" / "data" / "medical_signers"
SIGNED_SHARD = REPO_ROOT / "data" / "chestmnist" / "training_data" / "train-data-0.npz"


def signer_entry(file_stem: str, signer_id: str, role: str) -> dict[str, object]:
    public_key = (PUBLIC_SIGNER_DIR / f"{file_stem}-public.der").read_bytes()
    certificate = (PUBLIC_SIGNER_DIR / f"{file_stem}-certificate.der").read_bytes()
    return {
        "signer_id": signer_id,
        "active": True,
        "role": role,
        "display_name": signer_id,
        "public_key_der_hex": public_key.hex(),
        "certificate_der_hex": certificate.hex(),
        "certificate_fingerprint": hashlib.sha256(certificate).hexdigest(),
    }


def signer_snapshot() -> dict[str, object]:
    signers = [signer_entry(f"xray-device-{index}", f"XRAY_DEVICE_{index}", "XRAY_DEVICE") for index in range(5)]
    signers.append(signer_entry("radiologist-0", "RADIOLOGIST_0", "RADIOLOGIST"))
    return {
        "block_number": 100,
        "block_hash": "0x" + "11" * 32,
        "key_set_version": "6",
        "signers": signers,
    }


class DicomProvenanceTests(unittest.TestCase):
    def test_checked_in_shard_verifies_against_onchain_style_snapshot(self) -> None:
        result = verify_training_provenance(SIGNED_SHARD, signer_snapshot())
        self.assertEqual(result["verified_samples"], 13_078)
        self.assertEqual(result["active_device_keys"], 5)
        self.assertEqual(result["active_radiologist_keys"], 1)

    def test_modified_label_is_rejected(self) -> None:
        with np.load(SIGNED_SHARD, allow_pickle=False) as bundle:
            values = {name: bundle[name].copy() for name in bundle.files}
        values["labels"][0, 0] ^= np.uint8(1)

        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered.npz"
            np.savez_compressed(tampered, **values)
            with self.assertRaisesRegex(ValueError, "provenance verification failed"):
                verify_training_provenance(tampered, signer_snapshot())

    def test_modified_image_is_rejected(self) -> None:
        with np.load(SIGNED_SHARD, allow_pickle=False) as bundle:
            values = {name: bundle[name].copy() for name in bundle.files}
        values["images"][0, 0, 0] ^= np.uint8(1)

        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered.npz"
            np.savez_compressed(tampered, **values)
            with self.assertRaisesRegex(ValueError, "provenance verification failed"):
                verify_training_provenance(tampered, signer_snapshot())

    def test_revoked_device_key_is_rejected(self) -> None:
        snapshot = signer_snapshot()
        with np.load(SIGNED_SHARD, allow_pickle=False) as bundle:
            first_device = bytes(bundle["device_signer_ids"][0]).rstrip(b"\x00").decode("ascii")
        snapshot["signers"] = [signer for signer in snapshot["signers"] if signer["signer_id"] != first_device]
        with self.assertRaisesRegex(ValueError, "exactly 5 active X-ray devices"):
            verify_training_provenance(SIGNED_SHARD, snapshot)


if __name__ == "__main__":
    unittest.main()
