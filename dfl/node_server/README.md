# Node Server

`node_server` contains the Node.js orchestration logic that runs inside each DFL worker container.

It coordinates:

- smart-contract calls against `DeviceRegistry`, `AggregatorSelection`, and `GMStorage`,
- bound TDX registration (live Phala quote or explicit local-only mock),
- IPFS upload and download through Kubo or Pinata,
- global model and signature retrieval,
- RSA signature verification for global model artifacts,
- calls to the local Python neural-network HTTP service,
- authenticated worker-to-aggregator model transfer,
- aggregation state transitions and timeout handling.

The actual model training and aggregation implementation lives in `neural_network`. The Node server is the orchestration layer around that Python service.

## Runtime Role

Each worker process determines whether it is the current aggregator from the smart contracts:

- non-aggregator workers fetch the global model, verify its signature, train locally, and send their encrypted local model to the aggregator;
- the aggregator starts the authenticated HTTPS model receiver, verifies every
  worker commitment, runs federated averaging through the Python service, signs
  the new global model, uploads the encrypted model bundle to IPFS, and updates
  `GMStorage`.

## Participant Identity and TEE Action Authority

The logical participant account and its operational authority are deliberately
separate:

- `ACCOUNT_ADDRESS` identifies the participant in contribution scores,
  aggregator selection, model metadata, and the training protocol.
- `PRIVATE_KEY` is the bootstrap key for that logical account. In the Phala
  prototype it authorizes enrollment of the TEE action address and funds that
  address with Anvil test ETH; it does not authorize DFL state transitions.
- The worker derives a process-scoped secp256k1 action key from a dstack KMS
  secret and a fresh value held only in TEE memory, using the fixed context
  `vita-fl/participant-ethereum-action/v1`. Its address is bound into the
  registration `REPORTDATA`, the exact Compose evidence, and an enrollment
  authorization signed by the logical participant. A process restart produces a
  new action address and therefore requires fresh attestation; successful
  registration atomically revokes the prior address.
- `DeviceRegistry`, `AggregatorSelection`, and `GMStorage` resolve transaction
  senders through the registered action-address-to-participant mapping. Worker
  and aggregator operations therefore have to be signed by the action key of a
  currently attested worker.

The action key is derived just in time for signing and is not written to a
volume, environment variable, log, or Web3 wallet. It is nevertheless exposed
briefly to the measured application process by the dstack API; this design
reduces host-side key exposure but is not a non-exportable hardware-key API.

Each uploaded local model also carries an EIP-712 action-key commitment over
the round, selected aggregator, complete parent model bundle, plaintext model
hash, encrypted package hash, and worker nonce. The aggregator verifies the
transport signature and decrypted bytes locally, while `GMStorage` verifies the
action-key commitment before recording the submission or increasing the
worker's score. This prevents the aggregator from fabricating another worker's
accepted submission.

The EIP-712 submission digest is computed locally from the Compose-pinned
chain identifier and `GMStorage` address. The value returned by the RPC
contract call is used only as a consistency comparison and is never passed to
the action-key signer. Signed transactions likewise contain an explicit chain
identifier, nonce, destination and calldata before the private scalar is
derived; the signer refuses value transfers.

The action key also gates the selected aggregator's state transitions, endpoint
updates, penalties, global-model publication, round finalization and next
selection. It proves that those calls were authorized by the currently
registered worker process; it does not prove that the aggregate was computed
correctly from every accepted input.

Official worker Terraform resources disable SSH and user-provided pre-launch
code. Phala's SSH preference is supplied separately from the measured
`app_compose`, however, so absence of SSH is currently a launcher assumption
rather than an on-chain attestation guarantee for independently created CVMs.

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
- `PARTICIPANT_KEY_PROVIDER` (`dstack` is mandatory on Phala)
- `PARTICIPANT_KEY_STATE_PATH` (sealed RSA state; no plaintext private key)
- `LOCAL_ACTION_PRIVATE_KEY` (local mock tests only)
- `ACTION_KEY_MIN_BALANCE_WEI` and `ACTION_KEY_TARGET_BALANCE_WEI` (optional
  Anvil action-address funding thresholds)

See [compose.yml](../../compose.yml) and [.env](../../.env) for the local Docker wiring.
