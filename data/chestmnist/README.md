Place ChestMNIST artifacts here.

Expected layout for the current training pipeline:

- `data/chestmnist/chestmnist.npz`: original MedMNIST bundle downloaded from the official project
- `data/chestmnist/training_data/train-data-0.npz`, `train-data-1.npz`, ...: per-worker IID shards
- `data/chestmnist/test_data/test-data.npz`: shared evaluation split used by workers

The repository includes `scripts/generate_chestmnist_training_splits.py` to create the shard files from `chestmnist.npz`.
