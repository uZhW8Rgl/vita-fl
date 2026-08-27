Dataset-specific bootstrap models for the initial global model.

Expected files:

- `data/initial_gm/mnist/aggregated.bin`
- `data/initial_gm/chestmnist/aggregated.bin`

The local Docker flow signs and imports the matching file based on `DATASET_NAME`.

The ChestMNIST bootstrap uses PyTorch's fan-in-aware layer initialization with
`DFL_MODEL_SEED=42`. Its expected SHA-256 is
`8dfe51ae6de4a5772927efa216cc8b0383ac6aac711cc69000230b4b464e54cd`.
