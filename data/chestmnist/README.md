Place ChestMNIST artifacts here.

Expected layout for the current training pipeline:

- `data/chestmnist/chestmnist.npz`: original MedMNIST bundle downloaded from the official project
- `data/chestmnist/training_data/train-data-0.npz`, `train-data-1.npz`, ...: per-worker IID shards
- `data/chestmnist/test_data/test-data.npz`: shared evaluation split used by workers

The repository includes `scripts/generate_chestmnist_training_splits.py` to create the shard files from `chestmnist.npz`.

Every training sample is fail-closed provenance protected with two independent RSA-2048/SHA-256 signatures:

- one DICOM Creator RSA-style signature from one of five synthetic X-ray devices over a deterministic Explicit VR Little Endian byte stream containing the pixel data, SOP identities, acquisition identity, and equipment fields;
- one DICOM Authorization RSA-style signature from a synthetic radiologist over the 14 labels and references to the image SOP instance and creator-signature UID.

The corresponding X.509 certificates and DER public keys are deployed in `MedicalSignerRegistry`. Workers fetch an active-key snapshot from that contract at the beginning of every training round and verify every sample before loading the training tensors. An absent, revoked, malformed, or invalid signature rejects the complete shard.

The fixtures are DICOM-aligned synthetic provenance records, not claims that the original MedMNIST files contained native device signatures. Generate replacement fixture identities and re-sign all shards with:

```bash
python scripts/generate_chestmnist_signer_fixtures.py --force
PYTHONPATH=. python scripts/generate_chestmnist_training_splits.py --workers 500
```

Private fixture keys are generation-only inputs excluded from Git and from both runtime images. Public certificates live in `smart_contracts/data/medical_signers`.
