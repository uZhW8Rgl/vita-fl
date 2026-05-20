# ZK Inference

This directory exports a trained DFL model into an EZKL-compatible inference pipeline and generates a proof for a single MNIST image.

`neural_network` is the source of truth for:

- model class,
- binary parameter layout,
- MNIST normalization,
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

## Prepare a Random MNIST Sample

```bash
.venv/bin/python data/mnist_tools.py \
  --images data/mnist/data/t10k-images.idx3-ubyte \
  --labels data/mnist/data/t10k-labels.idx1-ubyte \
  --out-dir zk_inference/single_query \
  --input-json zk_inference/out/input.json
```

Without `--index`, the helper picks a fresh random MNIST image on each run and writes `zk_inference/single_query/selection.json`.

## Create a Single-Image Query

```bash
.venv/bin/python zk_inference/create_single_mnist_query.py \
  --model dfl/node_server/data/results_iid/aggregated.bin \
  --images data/mnist/data/t10k-images.idx3-ubyte \
  --labels data/mnist/data/t10k-labels.idx1-ubyte \
  --out-dir zk_inference/single_query \
  --input-json zk_inference/out/input.json
```

When `--index` is omitted, the query helper now chooses a random correctly classified MNIST test image from the search range. This creates:

- `zk_inference/single_query/single-image.idx3-ubyte`
- `zk_inference/single_query/single-label.idx1-ubyte`
- `zk_inference/single_query/single-image.pgm`
- `zk_inference/single_query/prediction.json`
- `zk_inference/out/input.json`

The current single-image helper is an evaluation helper: it can include ground-truth label metadata so local runs can report whether the prediction was correct. A deployment-style inference query should only require an input image and should not rely on the label file.

## Run EZKL

```bash
.venv/bin/python zk_inference/run_ezkl.py \
  --workdir zk_inference/out \
  --model model_logits.onnx \
  --data input.json
```

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
