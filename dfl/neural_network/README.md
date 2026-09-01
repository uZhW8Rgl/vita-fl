# Neural Network

This directory contains the active Python/PyTorch implementation used by the worker nodes.

It provides:

- the MNIST CNN model definition,
- binary model serialization for convolutional and linear layer weights,
- local training,
- encrypted model transfer helpers,
- deterministic equal-weight federated averaging,
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

The model is a compact CNN used by the DFL prototype:

```text
reshape(784) -> 1x28x28
conv(1->8, k=3, s=2, p=1) -> hidden activation
conv(8->16, k=3, s=2, p=1) -> hidden activation
flatten(16x7x7) -> 784
784 -> 32 -> 10
hidden activation -> logits
```

MNIST retains `tanh`; ChestMNIST uses LeakyReLU with slope `0.1` to avoid saturating the compact network.

The serialized model layout is:

```text
conv1.weight, conv1.bias, conv2.weight, conv2.bias, fc1.weight, fc1.bias, fc2.weight, fc2.bias
```

Values are stored as little-endian `float64` in the native tensor order of each layer.

## CLI

The CLI mirrors the original worker executable interface expected by the Node.js orchestration layer:

```bash
python -m neural_network.cli get_random_wb
python -m neural_network.cli train 30 <aggregator-public-key-der-hex>
python -m neural_network.cli server <client-limit> [private-key.pem]
python -m neural_network.cli client <aggregator-ip> <device-id>
python -m neural_network.cli aggregate <num-files>
```

## ChestMNIST

ChestMNIST uses 14 multi-label targets, so the final layer automatically expands from `10` to `14` outputs when `DATASET_NAME=chestmnist`.
Local training uses float32, AdamW (`lr=0.003`, weight decay `0.0001`), gradient clipping at `5`, and `BCEWithLogitsLoss` with task-wide inverse-prevalence weights capped at `10`. The weights come from the complete official training split rather than individual worker shards, so every worker optimizes the same objective. A deterministic round-and-worker seed controls batch order. The on-chain-compatible model remains serialized as little-endian float64.

The default run uses two local epochs and a constant learning rate. Longer evaluations can set `DFL_TRAIN_LR_SCHEDULE=late_cosine`; local multi-container runs additionally limit Torch to one thread per worker. Phala `tdx.small` workers do not need an explicit thread limit because each CVM already has one vCPU.

Create worker shards from the official `chestmnist.npz` bundle with:

```bash
.venv/bin/python scripts/generate_chestmnist_training_splits.py
```

The generator creates six signed IID shards by default, each containing 13,078
of the 78,468 training samples. It removes obsolete higher-numbered shards and
writes the task-wide label-count metadata.

The generator can also create a signed `CHESTMNIST-VAL-V1` research artifact
from the official validation split. Images that duplicate a training image or
an earlier validation image are removed before signing. The active aggregation
baseline does not consume this artifact; `test-data.npz` remains reporting-only.

## Active Aggregation Baseline

For every non-bootstrap round, the aggregator sorts the closed worker set by
address and computes an equal-weight arithmetic mean for every `float64`
state-dict tensor. This is model averaging with one equal contribution per
accepted client model; it is not weighted by local sample count. Non-finite
inputs or output parameters stop aggregation.

The former VITA-FL-specific Hybrid-R-style experiment remains isolated in
`hybrid_r.py` solely for auditability of historical research runs. The runtime
service does not import or call it, and active rounds use neither candidate
selection, a validation-loss gate, nor parent fallback.

## HTTP Service

The Docker worker starts the Python service and the Node.js worker process together. The service listens only on the container loopback interface at `127.0.0.1:8000` and exposes endpoints for:

- local training,
- client transfer,
- aggregation,
- starting and stopping the ZMQ aggregation server.

These control endpoints are not the cross-CVM model receiver. The Node.js process exposes the authenticated receiver on port `8001`; it verifies the signed round, aggregator, parent-model, worker, and ciphertext context before forwarding a package to the loopback-only Python service.

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
