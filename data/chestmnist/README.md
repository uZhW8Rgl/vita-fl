Place ChestMNIST artifacts here.

Expected layout for the current training pipeline:

- `data/chestmnist/chestmnist.npz`: original MedMNIST bundle downloaded from the official project
- `data/chestmnist/training_data/train-data-0.npz`, `train-data-1.npz`, ...: per-worker IID shards
- `data/chestmnist/training-metadata.json`: task-wide class counts used to derive fixed training-loss weights
- `data/chestmnist/validation_data/validation-data.npz`: signed, immutable reference split used only by
  the Hybrid-R aggregation policy
- `data/chestmnist/test_data/test-data.npz`: final evaluation split; it is not an input to Hybrid-R
  candidate selection

The repository includes `scripts/generate_chestmnist_training_splits.py` to create the shard files and
the validation artifact from `chestmnist.npz`.

Every training sample is fail-closed provenance protected with two independent RSA-2048/SHA-256 signatures:

- one DICOM Creator RSA-style signature from one of five synthetic X-ray devices over a deterministic Explicit VR Little Endian byte stream containing the pixel data, SOP identities, acquisition identity, and equipment fields;
- one DICOM Authorization RSA-style signature from a synthetic radiologist over the 14 labels and references to the image SOP instance and creator-signature UID.

The corresponding X.509 certificates and DER public keys are deployed in `MedicalSignerRegistry`. Workers fetch an active-key snapshot from that contract at the beginning of every training round and verify every sample before loading the training tensors. An absent, revoked, malformed, or invalid signature rejects the complete shard.

The fixtures are DICOM-aligned synthetic provenance records, not claims that the original MedMNIST files contained native device signatures. Generate replacement fixture identities and re-sign all shards with:

```bash
python scripts/generate_chestmnist_signer_fixtures.py --force
PYTHONPATH=. python scripts/generate_chestmnist_training_splits.py --workers 6
```

The validation artifact is derived only from the official `val_images` and `val_labels` arrays. Images
that also occur in the training split and repeated validation images are removed deterministically,
retaining the first validation occurrence. Its signed global sample identifiers are
`78468 + original_validation_index`, which keeps them disjoint from training identifiers
`0` through `78467`. Two training overlaps and five repeated validation images are removed, leaving
11,212 signed validation samples. The scalar split identifier is `CHESTMNIST-VAL-V1` and is included
in both the image and annotation signature preimages.

To regenerate only this artifact without rewriting the six training shards or the test split:

```bash
PYTHONPATH=. python scripts/generate_chestmnist_training_splits.py --validation-only
```

The embedded `semantic_sha256` is the canonical validation identity. Starting with the ASCII domain
`VITA-FL:CHESTMNIST:VALIDATION:SEMANTIC:V1` and a zero byte, SHA-256 frames each of the following
arrays in this fixed order: `dataset_split`, `images`, `labels`, `sample_indices`,
`device_signer_ids`, `device_signatures`, `radiologist_signer_ids`,
`radiologist_signatures`, and `provenance_version`. Each frame contains the length-prefixed field
name, NumPy `dtype.str`, rank, little-endian 64-bit dimensions, byte length, and contiguous C-order
bytes. This semantic digest is independent of ZIP metadata and compression-library differences.
For the checked-in artifact it is
`e4457c09ceeb203858e9b74232a4aa5b8852c623d85a63742a8751405d189d63`. The corresponding compressed
file-byte SHA-256 is
`9125cc0622896e8a8f0b4ee1d943b8bea4dda84b017324346475d3eebed0d9b4`; the semantic digest is the
canonical dataset identity.

Private fixture keys are generation-only inputs excluded from Git and from both runtime images. Public certificates live in `smart_contracts/data/medical_signers`.
