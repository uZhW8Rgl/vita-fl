# Architecture

This document summarizes the current end-to-end execution flow and the main component responsibilities.

The sequence diagram uses Mermaid so it renders directly on GitHub and can be exported or redrawn for the thesis document.

## End-to-End Flow

```mermaid
sequenceDiagram
  autonumber
  participant Compose as Docker Compose
  participant Anvil as Anvil
  participant SC as Smart Contracts
  participant IPFS as Kubo / IPFS
  participant W as Worker Nodes
  participant A as Agent
  participant ZK as ZK Inference

  Compose->>Anvil: Start local chain
  Compose->>IPFS: Start local IPFS node
  Compose->>SC: Deploy DeviceRegistry, AggregatorSelection, GMStorage
  SC->>SC: Deploy/configure TDX/DCAP attestation contracts
  SC->>IPFS: Pin initial global model metadata
  SC->>SC: Store initial model CID and signature CID

  Compose->>W: Start worker containers
  W->>SC: Register device with quote/public key context
  SC->>SC: Verify TDX/DCAP quote path
  W->>W: Train local MNIST model
  W->>W: Transfer local model artifacts
  W->>W: Aggregate submitted local models
  W->>IPFS: Upload aggregated model and RSA signature
  W->>SC: Update GMStorage with model CID and signature CID

  Compose->>A: Start agent after workers complete
  A->>SC: Read current model CID, signature CID, last aggregator
  A->>SC: Read aggregator public key from DeviceRegistry
  A->>IPFS: Fetch model and signature
  A->>A: Verify RSA signature over model artifact
  A->>ZK: Request single-image inference proof
  ZK->>ZK: Export model to ONNX and run EZKL
  ZK-->>A: Return prediction, witness, proof, verification status
```

## Component Responsibilities

| Component | Responsibility | Main entry point |
| --- | --- | --- |
| `compose.yml` | Local orchestration for infrastructure, contracts, workers, agent, and proof service | `docker compose up --build` |
| `smart_contracts` | DFL coordination, device registry, model metadata, and TDX/DCAP deployment scripts | `smart_contracts/starter_docker.sh` |
| `dfl/node_server` | Worker orchestration, contract interaction, timing logic, and tracing | `dfl/start_node_neural_network.sh` |
| `dfl/neural_network` | PyTorch training, local model transfer, aggregation, and serialization | `dfl/neural_network/cli.py` |
| `ipfs` | Local content-addressed storage for global model artifacts | Kubo API on `127.0.0.1:5001` |
| `agent` | Contract-based model retrieval, artifact fetching, signature verification, and proof orchestration | `agent/run_agent.py` |
| `zk_inference` | ONNX export, single-query generation, EZKL witness/proof generation, and verification | `zk_inference/server.py` |
| `observability` | Local logs, traces, metrics, and dashboarding | Grafana on `127.0.0.1:3000` |

## Design Notes

- `GMStorage` is treated as the source of truth for the active global model and signature CIDs.
- IPFS stores bytes, but authenticity is checked through on-chain metadata and the aggregator's registered public key.
- TDX/DCAP verification is represented by the on-chain attestation deployment and quote verification flow used during worker registration.
- The ZK inference path proves one selected MNIST inference over the exported model artifacts; it complements, but does not replace, model provenance checks.
- The local runtime is intended as a reproducible research prototype rather than a production deployment.
