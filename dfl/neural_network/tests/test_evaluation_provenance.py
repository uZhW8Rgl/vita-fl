"""Signed public fixture mutations, before training starts; no hardware claims."""

import copy
from unittest.mock import Mock

import numpy as np
import pytest

from dfl.neural_network import cli
from dfl.neural_network.dicom_provenance import verify_training_provenance
from dfl.neural_network.tests.test_dicom_provenance import SIGNED_SHARD, signer_snapshot


@pytest.fixture
def shard(tmp_path):
    with np.load(SIGNED_SHARD, allow_pickle=False) as source:
        values = {name: source[name][:2].copy() if source[name].ndim else source[name].copy() for name in source.files}
    path = tmp_path / "signed.npz"
    np.savez_compressed(path, **values)
    return path, values


@pytest.mark.parametrize(
    "case",
    [
        "pixels",
        "labels",
        "signer",
        "role",
        "certificate",
        "fingerprint",
        "mixed_snapshot",
    ],
)
def test_tampered_input_is_rejected_before_dataset_tensors_or_optimizer(shard, monkeypatch, case):
    path, values = shard
    snapshot = signer_snapshot()
    assert verify_training_provenance(path, snapshot)["verified_samples"] == 2
    device = bytes(values["device_signer_ids"][0]).rstrip(b"\x00").decode()
    entry = next(s for s in snapshot["signers"] if s["signer_id"] == device)
    other = next(s for s in snapshot["signers"] if s["role"] == "XRAY_DEVICE" and s is not entry)
    reason = "provenance verification failed"
    if case == "pixels":
        values["images"][0, 0, 0] ^= np.uint8(1)
    elif case == "labels":
        values["labels"][0, 0] ^= np.uint8(1)
    elif case == "signer":
        values["device_signer_ids"][0] = other["signer_id"].encode()
    elif case == "role":
        radiologist = next(s for s in snapshot["signers"] if s["role"] == "RADIOLOGIST")
        entry["role"], radiologist["role"] = "RADIOLOGIST", "XRAY_DEVICE"
        reason = "unapproved X-ray device"
    elif case in {"certificate", "fingerprint"}:
        entry["certificate_fingerprint"] = "ff" * 32
        if case == "certificate":
            entry["certificate_der_hex"] = other["certificate_der_hex"]
        reason = "certificate fingerprint mismatch"
    else:
        # Valid certificate and key from another snapshot entry, retaining the
        # original signer ID. This must not authenticate the original samples.
        entry.update(
            {
                k: copy.deepcopy(other[k])
                for k in (
                    "certificate_der_hex",
                    "public_key_der_hex",
                    "certificate_fingerprint",
                )
            }
        )
    np.savez_compressed(path, **values)
    tensors, optimizer = Mock(), Mock()
    monkeypatch.setattr(cli, "read_model_bin", lambda _: Mock())
    monkeypatch.setattr(cli, "is_multilabel_dataset", lambda: True)
    monkeypatch.setattr(cli, "train_dataset_paths", lambda: (path, path))
    monkeypatch.setattr(cli, "load_training_dataset", tensors)
    monkeypatch.setattr(cli, "build_training_optimizer", optimizer)
    with pytest.raises(ValueError, match=reason):
        cli.train_model(1, "unused-before-provenance", snapshot)
    tensors.assert_not_called()
    optimizer.assert_not_called()
