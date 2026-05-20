# Agent: Contract-Based Retrieval and ZK Inference

This folder contains a local agent layer around the DFL and ZK inference pipeline.

The normal flow is:

1. Read the current global model CID, signature CID, and last aggregator from `GMStorage`.
2. Fetch model and signature from the local IPFS/Kubo node.
3. Read the last aggregator's public key from `DeviceRegistry`.
4. Verify the model signature with RSA-SHA256 and PKCS1v15 padding.
5. Export the verified model to ONNX through `zk_inference`.
6. Create a single-image MNIST query.
7. Run EZKL to generate and verify a proof for the prediction.

The smart contracts are the source of truth. Direct IPFS scanning is kept only as a debug fallback.

## Install

From the repository root:

```bash
pip install -r zk_inference/requirements.txt
pip install -e dfl/neural_network
pip install -r agent/requirements.txt
```

`agent/requirements.txt` is only needed for LangChain, Ollama, and MCP mode. The deterministic pipeline can run without an LLM.

## Contract Source

The agent expects a running local chain and local IPFS API:

```bash
export RPC_URL=http://127.0.0.1:8545
export IPFS_API_URL=http://127.0.0.1:5001
export GM_STORAGE_ADDRESS=0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0
export REGISTRY_ADDRESS=0x5FbDB2315678afecb367f032d93F642f64180aa3
```

If values are omitted, the scripts try to read them from `.env` in the repository root. Docker-internal `http://anvil:8545` is mapped to `http://127.0.0.1:8545` for local host execution.

Fetch and verify only the on-chain model bundle:

```bash
.venv/bin/python agent/blockchain_source.py \
  --rpc-url http://127.0.0.1:8545 \
  --gm-storage-address 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  --registry-address 0x5FbDB2315678afecb367f032d93F642f64180aa3 \
  --ipfs-api-url http://127.0.0.1:5001 \
  --fetch \
  --verify
```

## Full Deterministic Run

```bash
.venv/bin/python agent/run_agent.py \
  --source contract \
  --rpc-url http://127.0.0.1:8545 \
  --gm-storage-address 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  --registry-address 0x5FbDB2315678afecb367f032d93F642f64180aa3 \
  --ipfs-api-url http://127.0.0.1:5001 \
  --skip-calibration
```

## Docker / Compose

`agent/Dockerfile` now packages only the agent/runtime logic.
`zk_inference/Dockerfile` packages the proof pipeline separately.

In `compose.yml`, `zk-inference` runs as its own container and `agent` starts
after `VM-0`, `VM-1`, and `VM-2` have completed successfully.
The agent still runs this command by default:

```bash
python agent/run_agent.py \
  --source contract \
  --rpc-url http://127.0.0.1:8545 \
  --gm-storage-address 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  --registry-address 0x5FbDB2315678afecb367f032d93F642f64180aa3 \
  --ipfs-api-url http://127.0.0.1:5001 \
  --skip-calibration
```

Inside Docker, loopback URLs are rewritten to the Compose services automatically,
and the agent forwards proof execution to `http://zk-inference:8090`.

Use a specific MNIST test image:

```bash
.venv/bin/python agent/run_agent.py --source contract --index 7 --skip-calibration
```

The output includes:

- downloaded model and signature paths in `agent/downloads`,
- signature verification status,
- prediction metadata from `zk_inference/single_query/prediction.json`,
- proof path, normally `zk_inference/out/proof.json`,
- EZKL witness, settings, and verification key paths.

## LangChain and Ollama Mode

The current LLM mode uses local Ollama, not an OpenAI API key:

```bash
ollama pull gemma4:e2b
ollama serve
```

Then:

```bash
export OLLAMA_BASE_URL=http://127.0.0.1:11434
export OLLAMA_MODEL=gemma4:e2b

.venv/bin/python agent/run_agent.py --llm --source contract --skip-calibration
```

In this mode LangChain can use local IPFS/RAG helpers and ZK tools exposed through MCP.

## MCP Server

Run the MCP server directly:

```bash
.venv/bin/python agent/zk_mcp_server.py
```

The MCP server does not reimplement proof logic. It wraps the existing scripts in `zk_inference`.

The exposed tools include `prepare_mnist_sample`, which extracts a fresh random MNIST input by default before the inference/proof flow continues.

## Debug IPFS Scan

For debugging old local artifacts, use:

```bash
.venv/bin/python agent/ipfs_rag.py \
  --api-url http://127.0.0.1:5001 \
  --root / \
  --fetch
```

`ipfs-scan` mode selects the newest complete model/signature pair. It first tries an exact filename match, then pairs the nearest timestamped signature within a configurable window.
