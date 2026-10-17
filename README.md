# Master Thesis Prototype: Trusted DFL and Verifiable Inference

[![CI](https://github.com/uZhW8Rgl/vita-fl/actions/workflows/ci.yml/badge.svg?branch=kms)](https://github.com/uZhW8Rgl/vita-fl/actions/workflows/ci.yml?query=branch%3Akms)

This repository contains a proof-of-concept implementation for trusted decentralized federated learning (DFL), model provenance, and verifiable single-image inference.

The current prototype combines:

- decentralized CNN training with multiple worker nodes,
- smart-contract-based coordination and model metadata,
- fixed per-round collection targets and deadlines, deterministic equal-weight
  federated averaging, and TEE-signed input/output statements with atomic
  model publication,
- local IPFS/Kubo storage for global model artifacts,
- TDX/DCAP quote verification through Solidity contracts,
- RSA signature verification for global model artifacts,
- an optional EZKL-based zero-knowledge inference backend for local experiments,
- AIR/TDX-bound ChestMNIST inference in a digest-pinned Phala TEE,
- SCITT-CCF registration and local verification of transparency receipts,
- workload-scoped random participant keys held in memory and private tmpfs,
- and a LangChain/Ollama agent using MCP tools, authenticated TEE calls, and evidence verification.


## Preview, recreate, or delete

Run these commands from this directory with your configured deployment profile:

```bash
bash phala/start.sh

# Preview changes without applying or deleting resources.
bash phala/start.sh --dry-run

# Delete the existing demo and create a fresh runtime.
bash phala/start.sh --recreate

# Delete the deployment, including leftover worker CVMs.
bash phala/start.sh --destroy
```

`--recreate` and `--destroy` erase the current run's chain state, IPFS data, and
training progress. After recreation, start training yourself in the UI.

## Architecture

The diagram shows the selected Phala deployment with native TEE inference.
Worker 0 performs training and serves inference inside the same worker image
and CVM. Aggregation rotates among admitted workers; the agent invokes bounded
tools and checks evidence. The optional [ZK backend](zk_inference/README.md)
is a separate code path and is disabled in this Phala profile.

### Runtime Component Overview

```mermaid
flowchart TB
  User["Participating organisation / user"]
  Ollama["Ollama LLM<br/>Separate Phala CVM"]
  QuoteAPI["Phala online quote verifier"]
  Auditor["Independent verifier / authorised owner"]

  subgraph Runtime["Shared demo runtime - Phala CVM"]
    UI["Web UI<br/>Training control, chat and evidence views"]
    Control["Control API<br/>Provisioning and run lifecycle"]
    Agent["AI agent + MCP tools<br/>Receiver authentication and evidence checks"]

    subgraph Chain["Anvil - demo Ethereum ledger"]
      Registry["DeviceRegistry<br/>RTMR3 replay, image policy and participant keys"]
      DCAP["On-chain TDX/DCAP verifier<br/>Quote authentication"]
      State["GMStorage + AggregatorSelection<br/>AggregationPolicy + MedicalSignerRegistry"]
    end

    IPFS["Kubo/IPFS<br/>Encrypted model bundles and recipient wrappers"]
    Log["SCITT / CCF log<br/>Signed statements and inclusion receipts"]
    Monitoring["Prometheus, Loki, Alloy and Grafana"]
  end

  subgraph Participants["Participants - separate CVMs using the same approved worker image"]
    subgraph Worker0["Worker 0 CVM - protected model processing"]
      Train0["Node.js + Python<br/>Local training / rotating aggregation"]
      Receiver["Native inference receiver<br/>Model preparation, jobs, AIR and Sello"]
    end
    OtherWorkers["Workers 1..N - individual CVMs<br/>Local training / rotating aggregation"]
  end

  User --> UI
  UI -->|Training controls| Control
  UI <-->|Chat and verified results| Agent
  Agent <-->|Language-model requests| Ollama
  Control -.->|Provision and manage| Participants

  Train0 -->|Attested registration| Registry
  OtherWorkers -->|Attested registration| Registry
  Registry -->|Appraise quote| DCAP
  Train0 <-->|Signed contributions and publications| State
  OtherWorkers <-->|Signed contributions and publications| State
  Train0 <-->|Encrypted local-model transfer| OtherWorkers
  Train0 <-->|Publish / retrieve encrypted models| IPFS
  OtherWorkers <-->|Publish / retrieve encrypted models| IPFS

  Agent -->|Resolve admitted receiver| Registry
  Agent <-->|Attestation-bound TLS, Sello token and PoP| Receiver
  Agent -->|Fresh quote appraisal| QuoteAPI
  Receiver -->|Resolve accepted publication| State
  Receiver -->|Fetch encrypted model; decrypt internally| IPFS
  Receiver -->|Publish signed AIR / Sello evidence| Log
  Agent -->|Publish session evidence; verify inclusion| Log

  State -.->|Accepted history and authority| Auditor
  Registry -.->|Public key snapshots| Auditor
  IPFS -.->|Retained encrypted artifacts| Auditor
  Log -.->|Evidence and receipts| Auditor
  Auditor -->|Fresh inference quote appraisal| QuoteAPI
  Participants -.->|Metrics and logs| Monitoring
  UI -.->|Dashboards| Monitoring

  classDef service fill:#fff5d6,stroke:#9b813c,color:#222;
  classDef workload fill:#e8f0ff,stroke:#5475a6,color:#222;
  classDef external fill:#f2f2f2,stroke:#777,color:#222;
  class UI,Control,Agent,Registry,DCAP,State,IPFS,Log,Monitoring service;
  class Train0,Receiver,OtherWorkers workload;
  class User,Ollama,QuoteAPI,Auditor external;
```

Blue nodes perform protected model processing; yellow nodes provide shared
runtime services. Solid arrows show runtime interactions, and dashed arrows
show lifecycle control, external auditing, or observability.

Membership follows successful attested registration, without preselected
participant addresses. The bootstrap workload snapshots all registered members
for the current training; later admissions do not rewrite its encrypted recipients.

The default ephemeral profile creates fresh action, RSA, Sello and AIR keys per
workload. The explicit onchain-KMS profile restores the same authenticated encrypted
key bundle after an authorized boot and reattests enrollment without resetting
contribution scores. Plaintext participant keys remain in memory/private tmpfs.
The KMS profile is locally tested. Hosted onchain KMS requires Base/Ethereum;
the [own dstack KMS profile](kms/README.md) uses the original contracts on Anvil.
The current Phala workspace blocks custom KMS URL updates, so its real key-release
path is not cloud-validated. See [KMS setup and validation limits](phala/README.md#shared-policy-onchain-kms-branch-kms).
Inference decrypts models inside Worker 0 and returns predictions and evidence
to the agent.
Independent checks use accepted ledger state, authentic policy references,
retained artifacts and statements; owner-content checks additionally require
the owner's disclosure capability.
