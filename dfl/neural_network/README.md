# Neural Network

This directory contains the active Python/PyTorch implementation used by the worker nodes.

It provides:

- the MNIST CNN model definition,
- binary model serialization for convolutional and linear layer weights,
- local training,
- encrypted model transfer helpers,
- federated averaging,
- global model signing,
- and an HTTP service used by `node_server`.

The old compatibility package has been flattened. The active imports are now directly under `neural_network`, for example:

```python
from neural_network.cli import FederatedCNN
from neural_network.service import main
```

By default, local training helpers use `DATASET_NAME=mnist` and resolve IDX files from `data/mnist/data` at the repository root.
Set `DATASET_NAME=chestmnist` to switch the worker training/evaluation path to `data/chestmnist/*.npz` shards.

## Model Layout

The model is a compact CNN used by the DFL prototype. MNIST uses tanh
activations; ChestMNIST uses LeakyReLU with slope `0.1` to avoid saturating
hidden units during federated training:

```text
reshape(784) -> 1x28x28
conv(1->8, k=3, s=2, p=1) -> dataset activation
conv(8->16, k=3, s=2, p=1) -> dataset activation
flatten(16x7x7) -> 784
784 -> 32 -> dataset activation -> logits (10 MNIST / 14 ChestMNIST)
```

The serialized model layout is:

```text
conv1.weight, conv1.bias, conv2.weight, conv2.bias, fc1.weight, fc1.bias, fc2.weight, fc2.bias
```

Values are stored as little-endian `float64` in the native tensor order of each layer.
Training uses `float32` internally and converts back to the unchanged binary
layout before encrypted transfer and FedAvg. ChestMNIST contains 26,830
parameters and occupies 214,640 serialized bytes; MNIST differs only in the
10-output final layer and occupies 213,584 bytes.

## CLI

The CLI mirrors the original worker executable interface expected by the Node.js orchestration layer:

```bash
python -m neural_network.cli get_random_wb
python -m neural_network.cli train 30 <aggregator-public-key-der-hex>
python -m neural_network.cli server <client-limit> [private-key.pem] --expected-round <round>
python -m neural_network.cli client <aggregator-ip> <device-address> --round-id <round>
python -m neural_network.cli aggregate <num-files>
```

## ChestMNIST

ChestMNIST uses 14 multi-label targets, so the final layer automatically
expands from `10` to `14` outputs when `DATASET_NAME=chestmnist`.
Its training path uses AdamW (`lr=0.003`, weight decay `0.0001`) and
`BCEWithLogitsLoss` with fixed task-wide class weights. The weights are derived
from the complete official training split and capped at `10`; they are not
estimated from individual worker shards. This keeps rare findings visible
without making one worker's local label distribution define the shared task.

The binary loss itself has no decision threshold. For reporting F1 and
confusion-matrix metrics, each pathology receives a threshold selected only on
the official Validation split by maximizing its validation F1. The default
`DFL_THRESHOLD_MAX_PREVALENCE_MULTIPLIER=2` restricts the selected threshold to
at most twice the label's Validation prevalence, preventing an isolated rare
positive from making most samples positive. Those thresholds are then applied
to the untouched Test split. Threshold-independent Macro-AUROC and Macro-AUPRC
are recorded as the primary ranking metrics, while F1 at the uncalibrated fixed
threshold `0.5` remains available as a separate diagnostic.

Bootstrap initialization and batch ordering are deterministic. The bootstrap
model uses PyTorch's layer-specific fan-in-aware initialization instead of the
previous blanket `[-0.5, 0.5]` distribution. The checked-in ChestMNIST
bootstrap was generated with model seed `42`; `DFL_MODEL_SEED` applies when
`get_random_wb` is explicitly used to create another bootstrap artifact.
`DFL_TRAIN_SEED` reproduces the per-worker, per-round batch order. The
optimizer settings can be overridden with `DFL_TRAIN_OPTIMIZER`,
`DFL_TRAIN_LEARNING_RATE`, `DFL_TRAIN_WEIGHT_DECAY`,
`DFL_GRAD_CLIP_NORM`, and `DFL_POS_WEIGHT_CAP`. The evaluation profile uses
`DFL_TRAIN_LR_SCHEDULE=late_cosine`: it keeps the base rate through source
round 20 and decays smoothly to `DFL_TRAIN_LR_FINAL_FACTOR=0.25` in the final
source round derived from `ROUND`. Non-finite losses, gradients,
received parameters, and output parameters are rejected before publication.
For a many-container CPU run, `DFL_TORCH_NUM_THREADS=1` and
`DFL_TORCH_INTEROP_THREADS=1` prevent every worker from creating its own large
CPU thread pool.

Create worker shards from the official `chestmnist.npz` bundle with:

```bash
python3 scripts/generate_chestmnist_training_splits.py --workers 25
```

The generator also writes `data/chestmnist/training-metadata.json`, which is
copied into every worker image and supplies the shared class statistics. All 25
IID shards together cover the official training split; each shard contains
3,138 or 3,139 examples.

## HTTP Service

The Docker worker starts the Python service and the Node.js worker process together. The service listens on port `8000` inside the container and exposes endpoints for:

- local training,
- client transfer,
- aggregation,
- starting and stopping the ZMQ aggregation server.

For local development:

```bash
python -m neural_network.service
```

## Install

From the repository root:

```bash
pip install -e dfl/neural_network
```

The core runtime dependencies are PyTorch, `cryptography`, and `pyzmq`.

Run the training regression suite from the repository root with:

```bash
DATASET_NAME=chestmnist PYTHONPATH=dfl python -m unittest discover \
  -s dfl/neural_network -p 'test_cli.py' -v
```
