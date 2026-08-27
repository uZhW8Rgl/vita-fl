Dataset-specific bootstrap models for the initial global model.

Expected files:

- `data/initial_gm/mnist/aggregated.bin`
- `data/initial_gm/chestmnist/aggregated.bin`

The local Docker flow signs and imports the matching file based on `DATASET_NAME`.
The ChestMNIST bootstrap is generated deterministically with seed `42` for the
8/16-channel model layout and contains 214,640 serialized bytes.
