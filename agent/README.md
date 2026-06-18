# Agent: Skill-Based Retrieval and ZK Inference

This folder contains the local agent layer around the DFL and ZK inference pipeline.

The implementation is now centered around three separate public agent skills:

1. `fetch_latest_verified_model_bundle()`
2. `generate_random_chestmnist_image(...)`
3. `generate_zk_inference_proof()`

The shared skill orchestration now lives in `agent/agent_skills.py`. MCP, the deterministic CLI path, and the LangChain/Ollama chat layer use that skill layer instead of each implementing the workflow independently.

Together they cover the normal flow in separate steps:

1. Read the current global model CID, signature CID, and last aggregator from `GMStorage`.
2. Fetch the encrypted model bundle and signature from the local IPFS/Kubo node.
3. Decrypt the model bundle for the intended recipient.
4. Read the last aggregator's public key from `DeviceRegistry`.
5. Verify the model signature with RSA-SHA256 and PKCS1v15 padding.
6. Export the verified model to ONNX through `zk_inference`.
7. Create a single-image query for the active dataset and write `zk_inference/out/input.json`.
8. Run EZKL against that prepared `input.json` to generate and verify a proof for the prediction.

The smart contracts are the source of truth. Direct IPFS scanning is kept only as a debug fallback.

## Install

From the repository root:

```bash
pip install -r zk_inference/requirements.txt
pip install -e dfl/neural_network
pip install -r agent/requirements.txt
```

`agent/requirements.txt` is only needed for LangChain, Ollama, and MCP mode. The deterministic pipeline can run without an LLM, but it now uses the same three-skill sequence internally for consistency.

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

Use a specific test image index:

```bash
.venv/bin/python agent/run_agent.py --source contract --index 7 --skip-calibration
```

Here `--index 7` affects the sample preparation step. `run_ezkl()` itself does not select an image again; it consumes the `input.json` already written by `create_single_image_query(...)`.

The output includes:

- downloaded model and signature paths in `agent/downloads`,
- signature verification status,
- prediction metadata from `zk_inference/single_query/prediction.json`,
- proof path, normally `zk_inference/out/proof.json`,
- EZKL witness, settings, and verification key paths.

## LangChain and Ollama Mode

The current LLM mode uses local Ollama, not an OpenAI API key:

```bash
ollama pull qwen3:0.6b
ollama serve
```

Then:

```bash
export OLLAMA_BASE_URL=http://127.0.0.1:11434
export OLLAMA_MODEL=qwen3:0.6b

.venv/bin/python agent/run_agent.py --llm --source contract --skip-calibration
```

In this mode LangChain should prefer the three public skills instead of manually composing low-level retrieval, verification, and proof steps.

## Persistent Chat Service

The Compose stack now includes:

- `ollama`: the Ollama API server on the internal Docker network
- `ollama-init`: a one-shot initializer that automatically pulls `qwen3:0.6b`
- `agent`: the persistent API/session backend
- `ui`: a separate frontend container that talks to the agent API and embeds Grafana

So other services can use the same model through `http://ollama:11434` without a manual `ollama pull`.
The model cache is stored in the repository folder `./ollama-data`, so it survives
`docker compose down --volumes`.

Start the complete chat agent with:

```bash
KEEP_ALIVE=0 docker compose up --build agent ui
```

Then open:

```bash
http://127.0.0.1:8089
```

The running `agent` stays alive and keeps chat sessions in memory, while the
separate `ui` container serves the browser frontend and waits for Grafana so the
embedded dashboard is available from the start.

For the embedded Grafana panel, use Grafana's **Share externally** feature and
set the generated link as `GRAFANA_EXTERNAL_DASHBOARD_URL` for the `ui` service.
This is separate from Grafana's generic embedding setting.

The agent can:

- answer directly through Ollama,
- fetch the current verified on-chain bundle through a dedicated skill,
- prepare one ChestMNIST sample through a separate skill,
- run the proof step through a separate EZKL skill,
- retain verified bundle and selected sample state across a chat session.

Inside Compose, the agent automatically uses `OLLAMA_BASE_URL=http://ollama:11434`.

## Terminal Modes

You can still use the same code without the UI.

Interactive terminal chat:

```bash
docker compose run --rm agent python agent/run_agent.py --llm --interactive --source contract --skip-calibration
```

Single custom prompt:

```bash
docker compose run --rm agent python agent/run_agent.py \
  --llm \
  --prompt "Explain the latest verified model bundle and then run one proof." \
  --source contract \
  --skip-calibration
```

## MCP Server

Run the MCP server directly:

```bash
.venv/bin/python agent/mcp_server.py
```

The MCP server does not reimplement proof logic. It wraps the existing scripts in `zk_inference`.

The main public MCP tools are now the skill-oriented entry points:

- `fetch_latest_verified_model_bundle()` resolves the current on-chain bundle, downloads it, decrypts it, verifies the signature, and exports the verified model.
- `generate_random_chestmnist_image(...)` prepares one ChestMNIST sample and writes the EZKL input artifacts.
- `generate_zk_inference_proof()` runs EZKL against the already prepared artifacts.

Only these three MCP tools are public. Lower-level functions such as the bundle fetch, query creation, and EZKL execution still exist internally as Python helpers, but they are no longer exposed as separate MCP tools.

## Debug IPFS Scan

For debugging old local artifacts, use:

```bash
.venv/bin/python agent/ipfs_bundle.py \
  --api-url http://127.0.0.1:5001 \
  --root / \
  --fetch
```

`ipfs-scan` mode selects the newest complete model/signature pair. It first tries an exact filename match, then pairs the nearest timestamped signature within a configurable window.
