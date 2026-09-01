# Master Thesis Prototype: Trusted DFL and Verifiable Inference

[![CI](https://github.com/uZhW8Rgl/Master-Thesis/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/uZhW8Rgl/Master-Thesis/actions/workflows/ci.yml)

This repository contains a proof-of-concept implementation for trusted decentralized federated learning (DFL), model provenance, and verifiable single-image inference.

The current prototype combines:

- decentralized CNN training with multiple worker nodes,
- smart-contract-based coordination and model metadata,
- immutable per-round aggregation thresholds, deterministic equal-weight
  federated averaging, and TEE-signed input/output statements with atomic
  model publication,
- local IPFS/Kubo storage for global model artifacts,
- TDX/DCAP quote verification through Solidity contracts,
- RSA signature verification for global model artifacts,
- EZKL-based zero-knowledge inference for a single dataset image,
- AIR/TDX-bound ChestMNIST inference in a digest-pinned Phala TEE,
- SCITT-CCF registration and local verification of transparency receipts,
- and a local LangChain/Ollama agent that orchestrates contract lookup, artifact verification, and proof generation.

## Repository Structure

- [compose.yml](./compose.yml): Docker Compose entry point for local end-to-end runs.
- [.env.example](./.env.example): Local runtime settings without worker identities.
- [.env.shared.example](./.env.shared.example): Shared configuration across local Anvil and Sepolia profiles.
- [.env.phala.anvil.example](./.env.phala.anvil.example): Chain-specific values for local Anvil runs.
- [.env.sepolia.example](./.env.sepolia.example): Chain-specific values for Sepolia runs.
- [data](./data): Shared local input artifacts and helpers. Generated RSA worker keys are local-only and ignored by Git.
- [observability](./observability): Grafana and Prometheus configuration.
- [smart_contracts](./smart_contracts/README.md): Focused Foundry project with DFL contracts and TDX/DCAP attestation deployment logic.
- [dfl/node_server](./dfl/node_server/README.md): Node.js orchestration layer used by each worker.
- [dfl/neural_network](./dfl/neural_network/README.md): Python/PyTorch CNN training, transfer, aggregation, and model serialization.
- [zk_inference](./zk_inference/README.md): ONNX export, single-image query creation, EZKL proof generation, and proof verification.
- [tee_inference](./tee_inference/README.md): PyTorch ChestMNIST inference service with deterministic CBOR and AIR/TDX evidence.
- [transparency_log](./transparency_log/README.md): Persistent Microsoft SCITT-CCF ledger in virtual mode.
- [agent](./agent/README.md): Local LangChain/MCP agent for contract-based model lookup, ZK inference, and verified TEE inference with SCITT registration.

## Architecture

The prototype is organized around a Docker Compose runtime. The diagrams below show the main runtime components and the end-to-end execution path from local infrastructure startup to verifiable inference.

### Runtime Component Overview

```mermaid
flowchart TB
  subgraph Compose["Docker Components"]
    direction TB

    subgraph Coordination["Coordination and Storage Layer"]
      subgraph Anvil["anvil (local Ethereum chain)"]
        direction LR
      
        AggregatorSelection["Aggregator Selection"]
        DeviceRegistry["Device Registry"]
        GMStorage["General Model Storage"]
        AutomataDcapTdxV4Attestation["TDX/DCAP V4 Attestation"]
      end
      IPFS["ipfs\nmodel + signature artifacts"]
    end

    subgraph AgentLayer["Agent Layer"]
      direction LR
      AgentMain["run_agent.py\nworkflow orchestration"]
      IPFSBundle["IPFS bundle\nselection + fetch"]
      Verify["artifact verification\nCID + RSA signature"]
      MCP["MCP / tool interface"]
    end

    subgraph Inference["Verifiable Inference Layer"]
      direction LR
      ZK["zk-inference\nHTTP service"]
      Pipeline[".bin -> PyTorch \n -> ONNX -> EZKL"]
      Artifacts["input.json\nwitness\nproof\nvk"]
    end

    subgraph Workers["DFL Worker Layer"]
      direction LR
      VM0["VM-0"]
      VM1["VM-1\nselected aggregator"]
      VM2["VM-..."]
    end

    subgraph Observability["Observability Layer"]
      direction LR
      Stores["Prometheus"]
      Grafana["Grafana"]
    end
  end


  GMStorage --> IPFS
  IPFS --> Pipeline

  VM1 <--> AggregatorSelection

  VM0 --> |"local model"| VM1
  VM2 --> |"local model"| VM1

  VM0 --> GMStorage
  VM2 --> GMStorage

  VM0 --> DeviceRegistry
  VM1 --> DeviceRegistry
  VM2 --> DeviceRegistry

  DeviceRegistry <--> AutomataDcapTdxV4Attestation

  IPFSBundle --> Verify
  AgentMain --> IPFSBundle
  IPFSBundle <--> GMStorage
  Verify --> MCP
  MCP --> ZK
  MCP --> AgentMain 

  ZK --> Pipeline

  Pipeline --> Artifacts

  Workers --> Stores
  Stores --> Grafana

  Artifacts --> MCP
```

## Reproducible Demo

For a clean local demo, start from a fresh stack and use Docker Compose for the base runtime:

```bash
docker compose --env-file .env.example --env-file .env.phala.anvil.example \
  down --volumes --remove-orphans
docker compose --env-file .env.example \
  --env-file .env.phala.anvil.example up --build --force-recreate
```

If Docker reports `Conflict. The container name "/ipfs_local" is already in use`, a separate Kubo/IPFS container is already running on your machine with the same fixed name and host ports. Stop and remove that old container before starting this stack:

```bash
docker stop ipfs_local
docker rm ipfs_local
```

The `.env` file is intentionally ignored by Git. For new setups, prefer split profiles over a single mixed file:

```bash
cp .env.shared.example .env.shared
cp .env.phala.anvil.example .env.phala.anvil
cp .env.sepolia.example .env.sepolia
./scripts/use-env-profile.sh anvil
```

This keeps common values in `.env.shared`, puts chain-specific values into `.env.phala.anvil` or `.env.sepolia`, and regenerates the active `.env` from the selected profile. That avoids dangerous mixes such as a Sepolia RPC together with Anvil contract addresses.

This rebuilds the base services, launches the local infrastructure, deploys the contracts, pins the public initial model, and leaves the deployment container successfully exited. Open the UI, select the desired configuration in **Training Setup**, and press **Start Training**. That action commits the exact roster and starts only the selected workers, the agent, and the inference service.

After the stack is up, the browser frontend is available at:

- `http://127.0.0.1:8089` for the thesis agent website with the chat UI and embedded Grafana dashboard
- `http://127.0.0.1:3300` for the standalone Grafana instance
