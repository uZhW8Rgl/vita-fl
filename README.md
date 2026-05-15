# Master Thesis Prototype: Trusted DFL and Verifiable Inference

[![CI](https://github.com/uZhW8Rgl/Master-Thesis/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/uZhW8Rgl/Master-Thesis/actions/workflows/ci.yml)

This repository contains a proof-of-concept implementation for trusted decentralized federated learning (DFL), model provenance, and verifiable single-image inference.

The current prototype combines:

- decentralized MNIST model training with multiple worker nodes,
- smart-contract-based coordination and model metadata,
- local IPFS/Kubo storage for global model artifacts,
- TDX/DCAP quote verification through Solidity contracts,
- RSA signature verification for global model artifacts,
- EZKL-based zero-knowledge inference for a single MNIST image,
- and a local LangChain/Ollama agent that orchestrates retrieval, verification, and proof generation.

## Repository Structure

- [compose.yml](./compose.yml): Docker Compose entry point for local end-to-end runs.
- [.env.example](./.env.example): Example runtime configuration; copy this to `.env` before running Docker Compose.
- [docs/architecture.md](./docs/architecture.md): End-to-end flow diagram and component responsibilities.
- [data](./data): Shared local input artifacts, including MNIST data, RSA worker keys, and the TDX quote used by the prototype.
- [observability](./observability): Grafana, Prometheus, Loki, Tempo, Promtail, and OpenTelemetry Collector configuration.
- [smart_contracts](./smart_contracts/README.md): Focused Foundry project with DFL contracts and TDX/DCAP attestation deployment logic.
- [dfl/node_server](./dfl/node_server/README.md): Node.js orchestration layer used by each worker.
- [dfl/neural_network](./dfl/neural_network/README.md): Python/PyTorch MNIST training, transfer, aggregation, and model serialization.
- [zk_inference](./zk_inference/README.md): ONNX export, single-image query creation, EZKL proof generation, and proof verification.
- [agent](./agent/README.md): Local LangChain/MCP agent for contract-based model retrieval, signature verification, and ZK inference.

## Reproducible Demo

For a clean local demo, start from a fresh stack and use Docker Compose as the single entry point:

```bash
cp .env.example .env
docker compose down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose up --build --force-recreate
```

The `.env` file is intentionally ignored by Git. Start from `.env.example`, review the values for your local setup, and keep machine-specific changes in `.env`.

This is the recommended demo run for the thesis prototype. It rebuilds the active services, launches the local infrastructure, deploys the contracts, runs the DFL flow, stores the new global model in IPFS, and updates the on-chain metadata.

## Demo Outcome

When the run completes successfully, you should have:

- a local Anvil chain with deployed DFL and attestation contracts,
- a local IPFS node with the global model and signature pinned,
- completed worker runs for training and aggregation,
- observability data in Grafana, Prometheus, Loki, and Tempo,
- and a ready-to-run verifiable inference path through `agent/run_agent.py`.

The most important generated outputs are:

- `agent/downloads/`: model and signature bundles fetched by the agent
- `zk_inference/out/`: ONNX, EZKL settings, witness, proof, and verification key
- `zk_inference/single_query/`: single-image MNIST input and prediction metadata
- `smart_contracts/broadcast/`: deployment metadata and latest contract addresses

## Recommended Local Run

The Docker setup is the primary way to run the DFL prototype:

```bash
cp .env.example .env
docker compose down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose up --build --force-recreate
```

This starts Anvil, Kubo/IPFS, observability services, deploys the smart contracts, registers workers through TDX quote verification, runs training, aggregates local models, stores the new global model and signature in IPFS, and updates the on-chain model metadata.

For local Anvil runs, `P256_MODE=native` is the default. The deployment script probes the native P-256 precompile at `0x0000000000000000000000000000000000000100` and uses it when available. If the current Anvil build does not expose the precompile, the script installs the local P-256 verifier at the same canonical address so the contracts still use the native verifier address. `P256_MODE=fallback` can be used to force the Daimo fallback verifier address instead.

The local stack starts:

- Anvil as local Ethereum-compatible chain,
- Kubo as local IPFS node,
- the `smart-contracts` deployment container,
- three DFL worker containers,
- Grafana, Prometheus, Loki, Tempo, Promtail, and OpenTelemetry Collector.

The root `.env` file provides the local timing, account, contract, and IPFS configuration. Use `.env.example` as the tracked template and keep local edits in `.env`. The local Docker flow also uses RSA keys from `data/rsa_keys`, MNIST data from `data/mnist`, and the TDX quote from `data/phala_tdx_quote`.

## Quick Verification

After `docker compose up`, these quick checks confirm that the core demo finished in a meaningful state:

1. Check that the main containers are healthy or completed:

```bash
docker compose ps
```

2. Query the current global model CID from `GMStorage`:

```bash
cast call 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  "getGlobalModel()(string)" \
  --rpc-url http://127.0.0.1:8545
```

3. Run the local verifiable inference agent:

```bash
.venv/bin/python agent/run_agent.py \
  --source contract \
  --rpc-url http://127.0.0.1:8545 \
  --gm-storage-address 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  --registry-address 0x5FbDB2315678afecb367f032d93F642f64180aa3 \
  --ipfs-api-url http://127.0.0.1:5001 \
  --skip-calibration
```

4. Inspect the resulting proof artifacts:

```bash
ls -lah zk_inference/out
ls -lah zk_inference/single_query
```

## Stack Flow

The sequence diagram in [docs/architecture.md](./docs/architecture.md) visualizes this flow.

1. Starts Anvil and Kubo.
2. Deploys core DFL contracts from `smart_contracts`.
3. Deploys Automata PCCS and TDX/DCAP attestation contracts.
4. Uploads PCCS collateral.
5. Pins the initial global model to IPFS and writes model metadata on-chain.
6. Starts workers.
7. Registers workers through on-chain TDX quote verification.
8. Runs local training and model transfer.
9. Aggregates submitted local models.
10. Signs and uploads the new global model and signature.
11. Updates `GMStorage` with the new model CID and signature CID.

## Observability

The local stack exposes:

- Grafana: <http://127.0.0.1:3000>
- Prometheus: <http://127.0.0.1:9090>
- Loki: <http://127.0.0.1:3100>
- Tempo: <http://127.0.0.1:3200>
- OpenTelemetry Collector OTLP HTTP: <http://127.0.0.1:4318>

In Grafana, open `Explore`, choose the `Tempo` datasource, and search:

```text
{ resource.service.name = "dfl-node-worker" }
```

Useful trace attributes include `dfl.round`, `dfl.role`, `dfl.account`, and `dfl.duration_ms`.

## Useful Commands

Open the local IPFS Web UI:

```text
http://127.0.0.1:5001/webui
```

Query the current global model from `GMStorage`:

```bash
cast call 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  "getGlobalModel()(string)" \
  --rpc-url http://127.0.0.1:8545
```

Query the current global model signature:

```bash
cast call 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  "getGlobalModelSignature()(string)" \
  --rpc-url http://127.0.0.1:8545
```

Clean the local stack:

```bash
docker compose -f compose.yml down --volumes --remove-orphans
```

## Agent and Verifiable Inference

After a DFL run, the agent can read the latest model metadata from the smart contracts, fetch the model and signature from IPFS, verify the signature against the last aggregator's registered public key, and run a verifiable single-image inference:

```bash
.venv/bin/python agent/run_agent.py \
  --source contract \
  --rpc-url http://127.0.0.1:8545 \
  --gm-storage-address 0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0 \
  --registry-address 0x5FbDB2315678afecb367f032d93F642f64180aa3 \
  --ipfs-api-url http://127.0.0.1:5001 \
  --skip-calibration
```

The result includes the selected input image, predicted label, proof artifact, witness, and verification status.

## Important Generated Artifacts

Some runs create local build and proof outputs:

- `smart_contracts/out/`: Foundry artifacts for the main Foundry project.
- `smart_contracts/lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs/out/`: Foundry artifacts for the PCCS subproject.
- `zk_inference/out/`: ONNX, EZKL settings, witness, proof, and verification key.
- `zk_inference/single_query/`: single-image MNIST input and prediction metadata.
- `agent/downloads/`: model and signature bundles fetched by the agent.

## GitHub Workflow

The repository now includes a GitHub Actions CI pipeline for:

- repository hygiene and Docker Compose validation,
- Python linting and formatting checks with Ruff,
- Node.js build and tests,
- Foundry contract builds,
- Docker image builds,
- and a runtime smoke test for the core containerized stack.

Recommended repository settings for `main` are documented in [`.github/BRANCH_PROTECTION.md`](./.github/BRANCH_PROTECTION.md).

## Notes

- The active neural-network implementation is Python/PyTorch in `dfl/neural_network`.
- `smart_contracts` is the active contract and attestation project.
- The healthcare setting is the motivating scenario. The concrete prototype uses MNIST as a reproducible proof-of-concept workload.
