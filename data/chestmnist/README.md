Place ChestMNIST artifacts here.

Expected layout for the current training pipeline:

- `data/chestmnist/chestmnist.npz`: original MedMNIST bundle downloaded from the official project
- `data/chestmnist/training_data/train-data-0.npz`, `train-data-1.npz`, ...: per-worker IID shards
- `data/chestmnist/test_data/test-data.npz`: held-out official Test split used for reported round metrics; it is not used to fit decision thresholds or other hyperparameters
- `data/chestmnist/training-metadata.json`: task-wide label counts used to derive the fixed multi-label loss weights

The official Validation arrays remain in `chestmnist.npz`. They are used to
calibrate one decision threshold per pathology; no Test labels are used for
that calibration.

The repository includes `scripts/generate_chestmnist_training_splits.py` to create 25 reproducible IID worker shards from `chestmnist.npz`.
When the generator is rerun with fewer workers, obsolete numerically indexed shard files are removed.
