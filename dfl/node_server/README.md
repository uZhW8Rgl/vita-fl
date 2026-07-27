# Node Server

`node_server` contains the Node.js orchestration logic that runs inside each DFL worker container.

It coordinates:

- smart-contract calls against `DeviceRegistry`, `AggregatorSelection`, and `GMStorage`,
- bound TDX registration (live Phala quote or explicit local-only mock),
- IPFS upload and download through Kubo or Pinata,
- global model and signature retrieval,
- RSA signature verification for global model artifacts,
- calls to the local Python neural-network HTTP service,
- worker-to-aggregator model transfer,
- aggregation state transitions and timeout handling.

The actual model training and aggregation implementation lives in `neural_network`. The Node server is the orchestration layer around that Python service.

## Runtime Role

Each worker process determines whether it is the current aggregator from the smart contracts:

- non-aggregator workers fetch the global model, verify its signature, train locally, and send their encrypted local model to the aggregator;
- the aggregator starts the ZMQ server, receives local models, runs federated averaging through the Python service, signs the new global model, uploads model and signature to IPFS, and updates `GMStorage`.

## Install

For local development:

```bash
cd dfl/node_server
npm install
```

## Run

The normal execution path is Docker, because the Node server expects environment variables for account keys, contract addresses, IPFS, and the Python service URL.

Inside Docker, workers are started through:

```bash
/dfl/start_node_neural_network.sh
```

For manual local experimentation:

```bash
cd dfl/node_server
npm run build
npm run start
```

## Important Environment Variables

- `ACCOUNT_ADDRESS`
- `PRIVATE_KEY`
- `DEVICE_ID`
- `REGISTRY_ADDRESS`
- `AGGREGATOR_ADDRESS`
- `GM_STORAGE_ADDRESS`
- `RPC_URL`
- `IPFS_PROVIDER`
- `KUBO_API`
- `KUBO_GATEWAY`
- `PYTHON_SERVICE_URL`
- `LOCAL_TDX_MOCK` (local Anvil only; never enable on Phala)
- the Registry-provisioned worker-image digest and role-policy hash (Phala;
  derived from canonical `app_compose` and enforced on-chain)
- `RSA_PRIVATE_KEY`
- `RSA_PUBLIC_KEY`

See [compose.yml](../../compose.yml) and [.env](../../.env) for the local Docker wiring.
