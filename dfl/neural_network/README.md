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

The model is now a compact MNIST CNN used by the DFL prototype:

```text
reshape(784) -> 1x28x28
conv(1->8, k=3, s=2, p=1) -> tanh
conv(8->16, k=3, s=2, p=1) -> tanh
flatten(16x7x7) -> 784
784 -> 32 -> 10
tanh -> logits
```

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
The loss also switches from `CrossEntropyLoss` to `BCEWithLogitsLoss`.

Create worker shards from the official `chestmnist.npz` bundle with:

```bash
.venv/bin/python scripts/generate_chestmnist_training_splits.py
```

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
