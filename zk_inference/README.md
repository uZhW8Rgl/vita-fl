# ZK Inference

This directory exports a trained DFL model into an EZKL-compatible inference pipeline and generates a proof for a single dataset image.

In the Phala deployment it is a separate TEE. It starts without a model and
provides a job API:

- `POST /v1/models/fetch` reads the current model references from the blockchain,
  downloads and decrypts the bundle, verifies the registered aggregator
  signature, and exports the model inside the TEE.
- `POST /v1/jobs` selects a ChestMNIST image and returns an opaque, model-bound
  job ID; no filesystem path leaves the service.
- `POST /v1/jobs/{job_id}/run-and-verify` generates and verifies the EZKL proof
  atomically and returns only verified metadata and artifact hashes.

All model, query, and proof artifacts live in the service's per-run runtime
directory and are discarded with the container.

`neural_network` is the source of truth for:

- model class,
- binary parameter layout,
- dataset normalization,
- and native prediction behavior.

## Pipeline

```text
aggregated.bin -> PyTorch state_dict -> ONNX -> EZKL witness/proof/verify
```

The preferred ONNX artifact for EZKL is `model_logits.onnx`, because the native worker model returns logits.

## Install

```bash
pip install -r zk_inference/requirements.txt
pip install -e dfl/neural_network
```

The editable `neural_network` install is optional when scripts are launched from the repository root, because the scripts add the local package to the import path.

## Export Model

Inspect a model:

```bash
.venv/bin/python zk_inference/export_model.py \
  --model dfl/node_server/data/results_iid/aggregated.bin \
  --inspect
```

Export PyTorch and ONNX artifacts:

```bash
.venv/bin/python zk_inference/export_model.py \
  --model dfl/node_server/data/results_iid/aggregated.bin \
  --out zk_inference/out
```

If no explicit model is given, the exporter tries the active aggregated model path first and then falls back to the newest `*-aggregated.bin` in `IPFS output`.

## Prepare a Random Dataset Sample

```bash
.venv/bin/python data/mnist_tools.py \
  --out-dir zk_inference/single_query \
  --input-json zk_inference/out/input.json
```

Without `--index`, the helper picks a fresh random image from the active dataset on each run and writes `zk_inference/single_query/selection.json`.

This preparation step also writes the `zk_inference/out/input.json` later consumed by the EZKL runner.

For explicit MNIST inputs:

```bash
.venv/bin/python data/mnist_tools.py \
  --images data/mnist/data/t10k-images.idx3-ubyte \
  --labels data/mnist/data/t10k-labels.idx1-ubyte \
  --out-dir zk_inference/single_query \
  --input-json zk_inference/out/input.json
```

For ChestMNIST after generating `data/chestmnist/test_data/test-data.npz`:

```bash
DATASET_NAME=chestmnist .venv/bin/python data/mnist_tools.py \
  --images data/chestmnist/test_data/test-data.npz \
  --labels data/chestmnist/test_data/test-data.npz \
  --out-dir zk_inference/single_query \
  --input-json zk_inference/out/input.json
```

## Create a Single-Image Query

```bash
.venv/bin/python zk_inference/create_single_mnist_query.py \
  --model dfl/node_server/data/results_iid/aggregated.bin \
  --images data/mnist/data/t10k-images.idx3-ubyte \
  --labels data/mnist/data/t10k-labels.idx1-ubyte \
  --out-dir zk_inference/single_query \
  --input-json zk_inference/out/input.json
```

When `--index` is omitted, the query helper chooses a random sample from the active dataset. For MNIST it prefers a correctly classified image from the search range. This creates:

- `zk_inference/single_query/single-image.pgm`
- `zk_inference/single_query/prediction.json`
- `zk_inference/out/input.json`

Depending on the dataset, the sample artifact is either:

- `zk_inference/single_query/single-image.idx3-ubyte` plus `single-label.idx1-ubyte` for MNIST
- `zk_inference/single_query/single-image.npy` plus `single-label.json` for ChestMNIST

The current single-image helper is an evaluation helper: it can include ground-truth label metadata so local runs can report whether the prediction was correct. A deployment-style inference query should only require an input image and should not rely on the label file.

ChestMNIST is supported in the query-preparation path, but a full Compose plus EZKL proof run for ChestMNIST should still be treated as an explicit integration check.

## Run EZKL

```bash
.venv/bin/python zk_inference/run_ezkl.py \
  --workdir zk_inference/out \
  --model model_logits.onnx \
  --data input.json
```

`run_ezkl.py` does not choose a sample by itself. It expects that `input.json` already exists, typically because it was written by the dataset/query preparation step above.

For faster local debugging:

```bash
.venv/bin/python zk_inference/run_ezkl.py \
  --workdir zk_inference/out \
  --model model_logits.onnx \
  --data input.json \
  --skip-calibration
```

The runner performs:

1. `gen_settings`
2. optional `calibrate_settings`
3. `compile_circuit`
4. `get_srs` or local `gen_srs` fallback
5. `setup`
6. `gen_witness`
7. `prove`
8. `verify`

The default fixed-point input and parameter scale is 8, the minimum accepted
by EZKL 23. Override it with `EZKL_SCALE` or `--scale`; values below 8 are
rejected before settings generation.

## Output Files

`zk_inference/out` normally contains:

- `model_state_dict_fp64.pt`
- `model_state_dict.pt`
- `model.onnx`
- `model_logits.onnx`
- `export_manifest.json`
- `input.json`
- `settings.json`
- `witness.json`
- `proof.json`
- `vk.key`

`model.onnx` includes a Softmax output for probability inspection. `model_logits.onnx` is the cleaner proof target.
