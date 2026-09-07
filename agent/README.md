# Agent: Skill-Based Retrieval and ZK Inference

This folder contains the local agent layer around the DFL and ZK inference pipeline.

The implementation exposes two separate three-step, job-based workflows.
TEE inference runs inside Worker 0's Phala TEE, while ZK inference runs in its
own Phala TEE:

1. `fetch_latest_verified_tee_model_bundle()`
2. `generate_random_tee_chestmnist_image(...)`
3. `run_and_verify_tee_inference(job_id)`
4. `fetch_latest_verified_zk_model_bundle()`
5. `generate_random_zk_chestmnist_image(...)`
6. `generate_and_verify_zk_inference_proof(job_id)`

In chat, `fetch the latest bundle` selects the TEE workflow directly. An
explicit ZK bundle request selects ZK and never silently switches to TEE.
Questions and requests involving several steps continue through the planner.
Deployments can set `ZK_INFERENCE_ENABLED=false` to omit ZK tools from the
planner and reject direct ZK calls before contacting a service. Phala sets this
flag while separate ZK inference lacks attested participant-key delegation;
local deployments retain both workflows by default.

The shared skill orchestration now lives in `agent/agent_skills.py`. MCP, the deterministic CLI path, and the LangChain/Ollama chat layer use that skill layer instead of each implementing the workflow independently.

Together they cover the normal flow in separate steps:

1. Read the current global model CID, signature CID, and last aggregator from `GMStorage`.
2. Fetch the encrypted model bundle and signature from the local IPFS/Kubo node.
3. Decrypt the model bundle for the intended recipient.
4. Verify that the encrypted payload and public recipient-key bundle agree on
   their round and recipient context before accepting the decrypted model.
5. Read the last aggregator's public key from `DeviceRegistry`.
6. Verify the model signature with RSA-SHA256 and PKCS1v15 padding.
7. Keep model and query artifacts inside the selected inference TEE, addressed
   only through an opaque job ID.
8. In the ZK service, export the verified model and create a single-image EZKL query.
9. Generate and verify the EZKL proof in one final, fail-closed tool call.

The smart contracts are the source of truth. Direct IPFS scanning is kept only as a debug fallback.

The third TEE toolcall runs and verifies one previously prepared job. It calls
the digest-pinned Phala TEE, verifies the AIR
signature and request/model/response hashes, compares REPORTDATA with the TDX
Quote V4, replays RTMR3, checks the measured `app_compose` and image digest,
registers the exact verified evidence bytes with SCITT-CCF, and verifies the
returned CCF receipt before returning the prediction. Both the raw evidence
bundle and transparent SCITT statement are stored for later inspection.

The third ZK toolcall similarly generates and locally verifies the EZKL proof,
builds a canonical CBOR statement containing the proof, public input, settings,
verification key, model identity, and artifact hashes, and submits it to SCITT.
The private witness is deliberately excluded. The tool returns only after the
CCF receipt has also been verified.

Both evidence types are appended to a per-container read model at
`/tmp/transparency-log/records.jsonl`. The agent serves a receipt-verified view
at `/transparency/` and its JSON data at `/api/transparency/records`. The UI
embeds that view through its authenticated same-origin proxy. This index is a
display cache; the signed transparent statements and their CCF receipts remain
the cryptographic evidence.

The digest-pinned endpoint and expected image digest are trusted process
configuration and are intentionally not MCP arguments. This first verifier
checks the AIR signature and all TDX report-data/RTMR3 workload bindings. It
also reports `dcap_collateral_verified: false`: Intel certificate-chain,
revocation, QE-identity, and TCB-status verification remains a separate DCAP
step until the existing on-chain verifier is connected to this tool.

When deployed through Terraform, chat readiness depends only on the separately
deployed, Bearer-authenticated Ollama service and its configured model. The
agent can therefore run before Worker 0 or the ZK-inference service is ready.
For TEE calls, an explicit endpoint is optional: the agent reads Worker 0's
authorized `DeviceRegistry` record and uses its REPORTDATA-bound HTTPS
`public_ip`. An explicit `TEE_INFERENCE_URL` remains available for diagnostics.
If an inference endpoint cannot be resolved, only that workflow fails; the chat
service and the other tools stay available.

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

In `compose.yml`, `zk-inference` runs as its own container. The Control API
starts the selected workers, `zk-inference`, and `agent` after **Start Training**;
the agent waits for the first finalized encrypted model rather than waiting for
all worker containers to exit.
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
ollama pull qwen3:1.7b
ollama serve
```

Then:

```bash
export OLLAMA_BASE_URL=http://127.0.0.1:11434
export OLLAMA_MODEL=qwen3:1.7b

.venv/bin/python agent/run_agent.py --llm --source contract --skip-calibration
```

In this mode LangChain should prefer the six public TEE and ZK skills instead of manually composing low-level retrieval, verification, and proof steps.

## Persistent Chat Service

The Compose stack now includes:

- `ollama`: the Ollama API server on the internal Docker network
- `ollama-init`: a one-shot initializer that automatically pulls `qwen3:1.7b`
- `agent`: the persistent API/session backend
- `ui`: a separate frontend container that talks to the agent API and embeds Grafana

So other services can use the same model through `http://ollama:11434` without a manual `ollama pull`.
The model cache is stored in the repository folder `./ollama-data`, so it survives
`docker compose down --volumes`.

Start the complete chat agent with:

```bash
docker compose up --build agent ui
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

The public MCP tools are the six job-oriented entry points:

- `fetch_latest_verified_tee_model_bundle()` makes the TEE obtain, decrypt, and
  verify the current model directly from the blockchain and IPFS references.
- `generate_random_tee_chestmnist_image(index=None)` creates a model-bound,
  opaque TEE job ID without exposing a container path.
- `run_and_verify_tee_inference(job_id)` performs TEE inference, all local
  AIR/TDX, RTMR3, Compose and image-policy checks, SCITT registration, and CCF
  receipt verification in one fail-closed call.
- `fetch_latest_verified_zk_model_bundle()` makes the ZK TEE obtain, decrypt,
  verify, and export the current on-chain model.
- `generate_random_zk_chestmnist_image(index=None)` creates an opaque,
  model-bound ZK job and its private query artifacts.
- `generate_and_verify_zk_inference_proof(job_id)` generates and verifies the
  EZKL proof in the ZK TEE before returning verified metadata and artifact hashes.

### Receiver-attested tool receipts

When `SELLO_REQUIRED=1`, every one of these six calls uses the receiver-attested
receipt profile derived from *Notarized Agents* (Sello v0.1). The agent presents
an Ed25519-signed compact JWS whose claims bind the owner's X25519 public key and
the permitted public SCITT URL. The independently deployed TEE or ZK service hashes the
canonical tool input and exact response bytes, HPKE-encrypts the CBOR receipt
body to that owner key, and signs the encrypted envelope as COSE_Sign1 with its
own Ed25519 key. Before releasing the response, the receiver submits the envelope
directly to SCITT-CCF and verifies the returned inclusion receipt. The agent then
verifies the receiver envelope and the returned transparent statement; it never
publishes the receipt itself.

Successful and failed receiver calls are recorded as `agent-tool-receipt`
entries. A missing, modified, incorrectly signed, wrong-service, wrong-token,
input/output-substituted, or non-included receipt fails closed. An inference
result is not released if direct SCITT publication fails.

Generate a coherent prototype key set with:

```bash
python phala/generate_sello_env.py --scitt-url https://CONTRACT_APP_ID-8000s.dstack-REGION.phala.network
```

Copy the resulting lines into `.env.phala.anvil`, then keep
`ENABLE_SELLO_RECEIPTS=true`. The service signing seeds are injected only into
their respective encrypted Phala app environments; the agent receives only the
corresponding public-key registry.

`SELLO_SCITT_URL` must use the contract-runtime app's public `-8000s` endpoint.
The trailing `s` enables TLS passthrough to SCITT-CCF's TLS listener, avoiding
an intermediate HTTP proxy. Do not use the Compose-only hostname
`transparency-log` from Worker 0's CVM or the separate ZK-inference CVM.

Container paths are internal and are not exposed as MCP arguments. Configure
the inference workflows with `TEE_INFERENCE_URL`, `ZK_INFERENCE_URL`,
`TEE_INFERENCE_IMAGE_DIGEST`, `CHESTMNIST_TEST_DATA`,
`TEE_INFERENCE_EVIDENCE_PATH`, `SCITT_URL`, `SCITT_DEVELOPMENT`,
`SCITT_SIGNER_DIR`, and `SCITT_TRANSPARENT_STATEMENT_PATH` when the defaults do
not match the deployment. `SCITT_DEVELOPMENT=1` is only appropriate for the
Virtual Mode prototype with its self-signed TLS certificate. In the combined
deployment, `TEE_INFERENCE_IMAGE_DIGEST` is the digest of `worker_image`, not a
separate inference-image digest.

## Debug IPFS Scan

For debugging old local artifacts, use:

```bash
.venv/bin/python agent/ipfs_bundle.py \
  --api-url http://127.0.0.1:5001 \
  --root / \
  --fetch
```

`ipfs-scan` mode selects the newest complete model/signature pair. It first tries an exact filename match, then pairs the nearest timestamped signature within a configurable window.
