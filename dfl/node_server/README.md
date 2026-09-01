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

## Round-0 Bootstrap

Contract deployment publishes only an unsigned, plaintext initial-model CID
and the worker-admission marker. Workers can therefore start and register their
TEE-generated keys without waiting for a pre-encrypted model bundle. The
Control API transports the exact selected roster to W0, but the immutable
`DeviceRegistry.runRosterDigest` and ordered committed roster are authoritative.
Before its first snapshot, W0 checks the MFS declaration against that on-chain
commitment and waits until every committed worker has registered and the roster
is frozen. It then reads every registered RSA key twice, rejects a changing
view, and atomically persists the exact addresses and DER/SPKI keys in its
protected participant-state directory. The snapshot contains only public keys
and is bound to the on-chain roster digest and order. A round-0 restart
validates the snapshot and each stored key byte-for-byte against the
now-immutable on-chain registration, but
does not reread MFS. A later mutable MFS marker therefore cannot wedge or
retarget recovery, while a locally modified key snapshot fails closed.
Subsequent model publications likewise encrypt for
the complete frozen committed roster rather than a mutable authorized-device
enumeration.

Round 0 performs no local training. W0 fetches the deployment-fixed initial
model CID, encrypts the model once, wraps the round key for every selected
worker, signs the encrypted bundle with its registered participant key, and
finalizes through the normal policy-bound aggregation path. Other workers wait
for round 1 without participating in timeout recovery. The initial plaintext
has no origin signature; recipients authenticate W0's encrypted bundle before
decrypting it. Learned global models retain their existing plaintext model
signatures in addition to the encrypted-bundle signature.

## Runtime Role

Each worker process determines whether it is the current aggregator from the smart contracts:

- non-aggregator workers fetch the global model, verify its signature, train locally, and send their encrypted local model to the aggregator;
- the aggregator starts the authenticated HTTPS model receiver, verifies every
  worker commitment, opens and closes the snapshotted on-chain aggregation
  policy, runs policy-identified equal-weight FedAvg over exactly that closed
  input set, signs the new global model, uploads the encrypted model bundle and
  companion signature/key-bundle objects to IPFS, signs the aggregation
  statement, and atomically finalizes the policy-bound publication in
  `GMStorage`.

## Participant Identity and TEE Action Authority

The logical participant account and its operational authority are deliberately
separate:

- `ACCOUNT_ADDRESS` identifies the participant in contribution scores,
  aggregator selection, model metadata, and the training protocol.
- `PRIVATE_KEY` is the bootstrap key for that logical account. In the Phala
  prototype it authorizes enrollment of the TEE action address and funds that
  address with Anvil test ETH; it does not authorize DFL state transitions.
- The worker deterministically derives an application-bound secp256k1 action
  key from a dstack KMS secret and a domain-separated context containing the
  logical participant, chain, and Registry identities. Its address is bound
  into the registration `REPORTDATA`, the exact Compose evidence, and an
  enrollment authorization signed by the logical participant. Restarting the
  same measured application derives the same action address and reuses the
  existing registration. A changed application or derivation context cannot
  impersonate that registration and must be attested and admitted separately;
  once a run roster is frozen, its action-key bindings cannot be rotated.
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
selection. For publication, the measured process signs an EIP-712 aggregation
statement over the immutable on-chain input root and count, round-policy and
algorithm hashes, plaintext model hash, encrypted output-bundle hash, CID tuple
hash, and nonce. `GMStorage` accepts that statement only for the currently
registered action key and combines publication, reward, and round advancement
in one transaction. The fixed worker path independently checks that every
staged file has an accepted on-chain commitment and refuses any count mismatch.
For non-bootstrap rounds, the policy fixes the equal-weight FedAvg algorithm
identity while the reserved validation-data hash and loss-gate fields are zero.
The Node.js process rejects any different policy before calling the Python
aggregator. The framed `outputBundleHash`, action-key-signed aggregation
statement, and finalized publication bind the same closed input set and output.

This provides attestation-based execution integrity and policy-bound
publication under the TDX/dstack, key-custody, and participant assumptions. It
is not an independent mathematical aggregation proof or a
universal Byzantine-robustness guarantee.

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

The worker does not trust a local `CLIENT_LIMIT` or submission deadline.
`AggregationPolicy`, reached through the compose-bound `GMStorage`, supplies
the immutable per-round threshold and absolute deadline.
- `LOCAL_TDX_MOCK` (local Anvil only; never enable on Phala)
- the Registry-provisioned worker-image digest and role-policy hash (Phala;
  derived from canonical `app_compose` and enforced on-chain)
- `PARTICIPANT_KEY_PROVIDER` (`dstack` is mandatory on Phala)
- `PARTICIPANT_KEY_STATE_PATH` (sealed RSA state; no plaintext private key)
- `LOCAL_ACTION_PRIVATE_KEY` (local mock tests only)
- `ACTION_KEY_MIN_BALANCE_WEI` and `ACTION_KEY_TARGET_BALANCE_WEI` (optional
  Anvil action-address funding thresholds)

See [compose.yml](../../compose.yml) and [.env](../../.env) for the local Docker wiring.
