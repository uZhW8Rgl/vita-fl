# Agent: Authenticated TEE Inference and MCP Tools

The agent calls the inference receiver inside Worker 0's combined, digest-pinned
`dfl-worker` image. Training and inference are processes in that same container
and CVM. The receiver uses the worker's RSA key internally to open the encrypted
model; the agent receives results and evidence, never that private key.

## Required authentication

Phala uses `TEE_TRANSPORT_MODE=ratls`. Before sending any Sello token, request
proof or inference payload, the agent requests fresh attestation on its TLS
connection. The quote binds the live TLS key, a client challenge and the
receiver's AIR/Sello identities. The agent verifies it with the trusted Phala
API, checks an explicit platform and workload policy, and sends the protected
request on that same verified connection. The evidence travels in a separate
HTTP attestation exchange, not an X.509 quote extension. A certificate or
`verified: true` without these bindings is insufficient.

Each protected request additionally requires the signed Sello token and a
proof signed with the agent's separately registered Ed25519 PoP key. The
receiver's subject-to-key registry is operator provisioned in measured
Compose; the sender cannot authorize a new key by including it in a request.
Tokens bind the subject, audience, permitted action and PoP key. Request proofs
bind the request to its intended target and content; job ownership remains
scoped to the authenticated subject. There is no anonymous inference mode or
`SELLO_REQUIRED` switch. `/v1/attestation` is the public challenge endpoint;
model actions, jobs and receipt retrieval remain protected.

The application listens on a Unix socket behind nginx inside the TEE. Use the
registered Phala `-8443s` TLS-passthrough endpoint. Redirects and unexpected TLS
connection changes fail closed. A new connection requires new evidence.

## Security configuration

Follow [the Phala deployment guide](../phala/README.md#mandatory-inference-authentication)
for exact policy provisioning and rollout steps.

| Variable | Purpose |
| --- | --- |
| `TEE_TRANSPORT_MODE` | `ratls` for attestation-bound TEE transport; `mtls` for the legacy PKI path |
| `AGENT_POP_SIGNING_SEED` | Agent-only Ed25519 seed, separate from the Sello issuer |
| `RATLS_ALLOWED_PLATFORM_MEASUREMENTS` | Nonempty JSON list of approved `mrtd`, `rtmr0`, `rtmr1`, `rtmr2` maps, each value 96 hex characters |
| `PHALA_ATTESTATION_VERIFY_URL` | Official Phala endpoint; custom endpoints are not supported by the deployment |
| `SELLO_OWNER_SUBJECT` | Subject whose public PoP key is provisioned in the receiver registry |
| `SELLO_TOKEN_ISSUER_SIGNING_SEED` | Owner's Ed25519 token-issuer seed |
| `SELLO_OWNER_HPKE_PRIVATE_KEY` | Owner's X25519 receipt-decryption key |
| `SELLO_SERVICE_REGISTRY` | Static receiver public-key registry; `{}` for TEE-only use |
| `SELLO_LOG_URLS` | Authorized public HTTPS SCITT destination |

Missing or malformed trust policy fails closed. Obtain the platform allowlist
from an independently approved OS reference; do not trust measurements merely
because the receiver supplied them. Phala authenticates the quote; the agent
still decides whether that quote describes the authorized image, deployment,
keys and endpoint. Availability and correctness of the configured Phala
verification API are additional trust dependencies.

The legacy mTLS path uses the [PKI operator workflow](../pki/README.md):
`PKI_CA_URL`, `PKI_ROOT_FINGERPRINT`, `PKI_SUBJECT`, `PKI_ENROLLMENT_TOKEN` and
`PKI_STATE_DIR`, followed by supervisor-provided certificate/key/CRL paths.
The supervisor skips enrollment for a TEE-only RA-TLS agent. When a legacy ZK
mTLS receiver is configured, retain PKI enrollment, renewal and revocation.
The Sello token remains mandatory in either transport mode.

## TEE workflow

The public TEE tools are:

1. `fetch_latest_verified_tee_model_bundle()` — the receiver resolves the
   finalized `GMStorage` references, downloads the encrypted bundle from IPFS,
   checks its provenance, and opens it with the worker's internal RSA key.
2. `generate_random_tee_chestmnist_image(index=None)` — creates a model-bound
   job owned by the authenticated agent. Paths and private query artifacts stay
   inside the inference container.
3. `run_and_verify_tee_inference(job_id)` — runs the prepared job and verifies
   the returned AIR evidence and transparency records before returning results.

The agent checks the AIR signature, request/model/response hashes, REPORTDATA
binding, measured `app_compose`, expected combined-image Digest, and RTMR3 replay.
In `ratls` mode it also sends the separate AIR quote to Phala for cryptographic
verification and requires the AIR session binding to match the verified TLS
session, TLS public key and AIR key. It reports `dcap_collateral_verified: true`
with verification metadata after the delegated Phala check succeeds. This flag
means verification by the trusted online Phala service; the agent does not
independently validate Intel collateral, and an unsigned JSON API response is
not an offline signed Phala attestation. Legacy `mtls` AIR verification still reports
`dcap_collateral_verified: false`. Worker admission remains a separate on-chain
DCAP step; the admitted `DeviceRegistry` record supplies the Sello receiver key
and HTTPS origin.

`TEE_INFERENCE_URL` can explicitly select an HTTPS origin, but it must still
match the admitted receiver record. Otherwise the agent discovers Worker 0's
registered origin. `TEE_INFERENCE_IMAGE_DIGEST` refers to the combined worker
image, which also contains the inference code.

For each tool call the receiver hashes the exact input and output, encrypts a
Sello receipt to the owner's X25519 key, signs it with its own Ed25519 key, and
publishes it directly to SCITT. The agent verifies that receipt and its inclusion
proof. Missing, substituted, incorrectly signed, or unpublished receipts fail
closed. The separate AIR evidence bundle is also registered and verified with
SCITT. Neither path releases a successful verified result on publication failure.

The receipt-verified display index at `/tmp/transparency-log/records.jsonl`
feeds `/transparency/` and `/api/transparency/records`. This index is a cache;
signed statements and their CCF receipts remain the evidence.

## ZK workflow status

The MCP definitions also retain these three ZK tools:

- `fetch_latest_verified_zk_model_bundle()`
- `generate_random_zk_chestmnist_image(index=None)`
- `generate_and_verify_zk_inference_proof(job_id)`

The current Phala deployment disables them. A separate ZK receiver lacks
attested participant-key delegation, and its former plaintext HTTP entrypoint
is disabled. Setting a flag does not restore that unsafe transport. Its local
proof and orchestration libraries remain available for tests. The historical
`http://zk-inference:8090` configuration is not an authenticated deployment path.

## Install and run

From the repository root, install Python 3.12 dependencies:

```bash
python -m pip install -r agent/requirements.txt
```

Configure the RA-TLS/Sello policy above before starting a TEE-only agent.
The Phala image starts through `pki.runtime`; the `step` CLI is needed only for
the legacy mTLS path. For local execution with the selected transport:

```bash
python -m pki.runtime run agent -- python agent/run_agent.py \
  --llm --source contract --serve
```

Configure `RPC_URL`, `REGISTRY_ADDRESS`, `ACCOUNT_ADDRESS` (Worker 0's participant
identity), `GM_STORAGE_ADDRESS`, and `IPFS_API_URL` for the deployment. LLM mode
also needs a reachable Ollama service and `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and
its configured access token. A model or inference outage affects that workflow;
it does not make unverified tool responses acceptable.

For terminal chat, use `--llm --interactive`; for a single instruction use
`--llm --prompt "..."`. To run the local stdio MCP server with the same security
configuration:

```bash
python -m pki.runtime run agent -- python agent/mcp_server.py
```

The MCP server does not expose a public network port. These receiver checks
protect agent-to-inference calls; access to a deployed chat UI is a separate
boundary handled by its authenticated frontend/proxy.

The low-level read-only helpers `agent/blockchain_source.py` and
`agent/ipfs_bundle.py` remain available for inspecting model references and old
local artifacts. They do not provide an alternative inference endpoint.

## Validation and rollout

Run agent tests with `PYTHONPATH=. python -m unittest discover -s agent/tests -v`.
The `transport_security` and `pki` pytest suites additionally exercise TLS,
PoP replay rejection, attestation policy failures, identity binding, revocation,
and real CA enrollment/renewal. Hardware quotes and the Phala verifier are
mocked in local attestation tests; a live configured Phala deployment is still
required to validate the complete hardware path. The CI certificate
lifecycle job installs the pinned Smallstep binaries and nginx so the real CA
and private-proxy integration checks run; these checks explicitly skip locally
when their required binaries are absent.

This change requires newly built agent and combined-worker images. Worker
admission must use the new image Digest and freshly derived role-policy hashes.
Updating source files alone does not change existing CVMs. No live deployment
was performed as part of this implementation.
