# Phala/dstack Worker Image Attestation

This directory contains deployment templates for the contract runtime and the
DFL worker TEEs. Worker 0 additionally serves TEE inference from the same
digest-pinned `dfl-worker` image and the same CVM; there is no separate
TEE-inference Phala app.

## Terraform Deployment

This directory also contains a Terraform scaffold for deploying the DFL prototype to Phala Cloud with the official provider `phala-network/phala` version `0.2.0-beta.3`.

Files:

- `main.tf`: provider, one `phala_app` for the contract runtime TEE, worker apps, optional `phala_ssh_key`, and optional `phala_cvm_power`
- `variables.tf`: Phala and worker runtime configuration
- `outputs.tf`: deployed app metadata
- `terraform.tfvars.example`: values you can copy into `terraform.tfvars`
- `dstack-compose.contracts.phala.tftpl`: Terraform-rendered compose policy for the contract-runtime TEE
- `dstack-compose.worker.phala.tftpl`: Terraform-rendered compose policy for a
  worker TEE; its Worker 0 rendering also enables the co-located inference
  process
- `dstack-compose.contracts.template.yml`: manual compose policy for the contract-runtime TEE
- `dstack-compose.template.yml`: manual Worker 0 compose policy, including the
  co-located inference process

Copy the complete example, replace every placeholder (including the Phala API
key and UI login), and start the complete deployment with one command:

```bash
cp .env.phala.anvil.example .env.phala.anvil
bash phala/start.sh
```

On a fresh account the launcher first creates the contract-runtime endpoint,
then reapplies the runtime with the derived RPC, Kubo API, and Kubo gateway
URLs used by dynamically created worker TEEs. The application bootstrap is
deliberately separated from deployment:

1. the runtime deploys the contracts and publishes
   `/runtime/contracts.json` (including `aggregation_policy_address`) and
   `/runtime/admission-ready.json`, then exits successfully;
2. Start Training validates the measured Registry, AggregatorSelection, and
   GMStorage addresses against the live chain, derives the AggregationPolicy
   directly from GMStorage, requires an empty worker deployment, and
   owner-commits the exact ordered roster with W0 first. If the published
   manifest is available, it is cross-checked as additional metadata;
3. only then does it launch those worker TEEs; only committed identities can
   complete DCAP registration, and the last registration freezes the roster;
4. W0 reads the frozen keys from `DeviceRegistry`, encrypts the public,
   unsigned initial model for that fixed roster, and finalizes contract round
   0. The other workers wait for this transition, and federated training starts
   in round 1.

This ordering leaves deployment independent of a future worker count and never
provisions a participant RSA key outside its attested workload. The roster
marker transports readiness only and is not a security authority: W0 requires
the exact immutable on-chain roster, its digest and every registered public
key. On restart, W0 validates its protected public-key snapshot against that frozen
on-chain state without rereading mutable MFS metadata.

Dynamic worker TEEs send operational training events to the Control API over
its Phala `8091` endpoint. Each event is signed by the worker's configured
Ethereum account, checked against the fixed W0--W499 inventory, protected
against nonce replay, and then exposed to Prometheus. This is how the embedded
training dashboard receives starts, model transfers, aggregation results,
evaluation metrics, and worker transaction costs even though every worker has
its own isolated CVM filesystem. These operational signatures are not a
replacement for the worker's TDX/DCAP registration proof.

The contract-runtime app persists only the dynamic-worker Terraform ownership
state in the `dynamic-worker-state` volume. This does not preserve Anvil,
training, Prometheus, or Grafana run data. It allows the Control API to find,
update, and destroy Worker CVMs after its own container restarts, preventing
stopped but unmanaged Phala apps from accumulating.

In Phala mode, resetting or reinitializing the contract stack first destroys
all dynamic worker apps, clears evaluation artifacts and signed runtime
telemetry, restarts the ephemeral Prometheus and Anvil containers, and then
reruns the existing `smart-contracts` container. Restarting Prometheus clears
its tmpfs-backed time-series database, while restarting Anvil restores its
complete genesis state, including the standard CREATE2 deployer. The Control
API receives only the contract-runtime Docker socket for these operations; it
does not mount or rewrite the measured application Compose file. Worker TEEs
continue to use the externally exposed restricted RPC proxy. A worker Compose is
immutable after attested registration. Changing measured worker configuration
such as `EPOCH` or `ROUND` requires the fresh reset path instead of an in-place
app update.

The **Federated Rounds** count shown in the UI is the number of actual client
training rounds. Contract round 0 is a bootstrap-only model rollover. The
Control API persists the user selection separately as
`REQUESTED_TRAINING_ROUNDS`; legacy `ROUND` mirrors that same user-facing
value. A worker receives a successful-completion target equal to the requested
count plus one. This target is not the raw on-chain attempt number, which can
advance during timeout recovery without completing a training round.

Immediately before the Control API scales the selected workers for a training
start, it validates the measured Registry, AggregatorSelection, GMStorage, RPC
chain, deployed bytecode, and the contracts' on-chain links. It reads
`aggregation_policy_address()` from that GMStorage and checks the policy's code
and GMStorage backlink. A readable `/runtime/contracts.json` is cross-checked,
but losing this mutable hand-off after the deployment container exits cannot
block setup recovery. The Control API then uses the encrypted Anvil-owner
`ETH_WALLET_PRIVATE_KEY` against the internal `RPC_URL` to configure the policy
and commit the exact ordered roster. The required submission count is the selected
`client_limit`; the submission window is
`ceil(MODEL_SUBMISSION_DEADLINE_MS / 1000)` seconds. Both transactions must be
mined successfully before any worker is created. If deployment is interrupted
after commitment, **Resume Start** may reapply only the same saved
configuration and exact roster; a different roster requires a reset.

`CLIENT_LIMIT` and `MODEL_SUBMISSION_DEADLINE_MS` are therefore owner-controlled
contract-runtime/Control-API inputs, not measured Worker-Compose security
inputs. Workers obtain the immutable per-round threshold and deadline from the
AggregationPolicy reached through the compose-measured GMStorage contract.
`EPOCH` and `ROUND` remain measured worker inputs.

The Training Setup view can export the current evaluation and transaction-cost
data as a ZIP archive of CSV tables. In Phala mode, the Control API merges the
signed in-memory worker telemetry with CSV artifacts from the shared evaluation
volume. The export includes exact gas-used and Wei totals, receipt-implied Gwei
and ETH fees, and explicitly configured EUR or USD estimates. Optional
Mainnet-reference estimates remain in separate fields; the corresponding gas
price, exchange rates, source identifiers, and UTC observation times are passed
from the selected Phala environment file. Configure them through
`ETH_EUR_PRICE`, optional `ETH_USD_PRICE`, `EXCHANGE_RATE_SOURCE`,
`EXCHANGE_RATE_TIMESTAMP_UTC`, and the analogous `REFERENCE_*` gas-price
variables shown in `.env.phala.anvil.example`.

If you already keep the deployment values in repository-root env files, you can use the helper wrapper instead of duplicating secrets into `terraform.tfvars`:

```bash
bash phala/tf-env.sh init
bash phala/tf-env.sh plan -input=false
bash phala/tf-env.sh apply
```

The wrapper uses `.env.phala.anvil` by default and can be pointed at another
complete Phala environment file with `PHALA_ENV_FILE=/path/to/file`. It fails
if that file is absent. `.env.phala.anvil.example` is the complete tracked
W0--W499 template; copy it to `.env.phala.anvil` and replace the deployment
placeholders as needed. The Phala path does not read worker identities from
`.env.example` or `data/rsa_keys`.

The wrapper reads these values from the selected env file:

- `PHALA_CLOUD_API_KEY`
- `UI_BASIC_AUTH_USERNAME` and `UI_BASIC_AUTH_PASSWORD` when the UI is enabled
- mandatory `Wn_ACCOUNT_ADDRESS`, `Wn_PRIVATE_KEY`, and `Wn_DEVICE_ID` values
  for every configured slot from W0 through W499

The Ethereum accounts and device identifiers are unchanged. Per-worker
`Wn_RSA_PRIVATE_KEY` and `Wn_RSA_PUBLIC_KEY` values are not deployment inputs:
each worker creates its participant RSA key inside its TEE. Any existing W0 RSA
fixture is only retained for legacy prototype components; it neither signs the
public initial model nor replaces the worker/aggregator key registered by
`DeviceRegistry`.

It also forwards the current Anvil/DFL profile settings into Terraform, including:

- `WORKER_COUNT`, `MAX_DYNAMIC_WORKERS`, and `ANVIL_ACCOUNT_COUNT`
- `INITIAL_GM_CID`
- `CLIENT_LIMIT`, `EPOCH`, `ROUND`
- `DFL_MODEL_SEED`, `DFL_TRAIN_SEED`, `DFL_TRAIN_OPTIMIZER`, and
  `DFL_TRAIN_LEARNING_RATE`
- `DFL_TRAIN_LR_SCHEDULE`, `DFL_TRAIN_LR_DECAY_START_ROUND`, and
  `DFL_TRAIN_LR_FINAL_FACTOR`
- `DFL_TRAIN_WEIGHT_DECAY`, `DFL_GRAD_CLIP_NORM`, and `DFL_POS_WEIGHT_CAP`
- `MODEL_SUBMISSION_DEADLINE_MS`, `GM_UPDATE_TIMEOUT_MS`, `GM_UPDATE_TIMEOUT_LOOPS`
- `AGGREGATION_UPDATE_ESTIMATE_MS`, `GM_UPDATE_POLL_MS`
- `DATASET_NAME`
- `PCCS_FMSPC`, `PCCS_FETCH`, `PCCS_TEE`, `P256_MODE`
- `DEPLOY_TDX_V4_DCAP`, `VERIFY_TDX_QUOTE_ONCHAIN`, `AGGREGATOR_TIMEOUT_REPORT_PERCENT`
- `TDX_REFERENCE_QUOTE_PATH` for the owner-reviewed dstack base-runtime quote; it is mandatory when DCAP verification is deployed and must be distinct from `PCCS_QUOTE_PATH`

The wrapper validates the complete fixed inventory, splits it into numbered
JSON-array chunks below 60,000 bytes each, and supplies those chunks through a
temporary mode-0600 Terraform variable file. This avoids the Linux size limit
for a single environment entry. The Control API reassembles the chunks in
numeric order and rejects incomplete, ambiguous, or malformed input. The
temporary file is removed when Terraform exits.

The selectable pool contains W0 through W499, while the default initial UI
selection remains three workers. Starting hundreds of simultaneous Phala CVMs
is still subject to the account quota and cost. A target platform with a
stricter aggregate environment/request limit may require an external encrypted
inventory store.

The wrapper is the supported deployment entry point because it validates and
transports the complete credential inventory. Do not maintain a second copy of
worker identities in `terraform.tfvars`.

Notes:

- The provider's `env` attribute encrypts Ethereum wallet and component
  secrets for the target Phala app. The measured/public Compose contains only
  environment-variable names, never their values.
- The Control API receives the same encrypted Anvil-owner key as the contract
  deployer, plus only the internal `http://anvil:8545` write endpoint. Dynamic
  worker CVMs continue to receive the externally exposed restricted RPC URL.
- Terraform still records sensitive `env` inputs in state. Local state, state backups, `terraform.tfvars`, and exported `app_code.txt` are ignored; use an encrypted, access-controlled remote backend for non-demo deployments.
- `public_logs` defaults to `true` for this observable Anvil demo deployment. Do not log secrets when adapting it for production.
- Worker and smart-contract images contain no private keys. The prototype EVM
  keys remain the unchanged public Anvil fixtures and must be replaced for
  production or real funds.
- The public Anvil account remains the logical worker identity and funds its
  action address, but it is not accepted as DFL transaction authority. At each
  worker-process start, the application combines a domain-separated dstack KMS
  secret with the logical participant, chain, and registry identities to
  reconstruct an application-bound secp256k1 action key. Its address is
  included in REPORTDATA during the first registration. An exact reboot reuses
  that registration; any mismatching active registration fails closed before
  quote generation, and the contract independently rejects a second one.
- `worker_image` must stay pinned to a `sha256` digest. Terraform also renders
  training-only and Worker-0-with-inference policy references from
  `dynamic-workers/worker-compose.tftpl`. The contract runtime derives their
  `workerPolicyHash` values through `DeviceRegistry` and owner-provisions both
  before publishing `/runtime/admission-ready.json`.
- `smart_contracts_image` should also be pinned to a `sha256` digest when you want the contract-runtime TEE to be reproducible.
- The Terraform scaffold separates `smart-contracts` and each `dfl-worker`
  into different Phala apps / TEEs. It does not deploy a separate
  `tee-inference` app.
- Every worker generates a random RSA-3072 key with public exponent 65537
  inside its TEE. A domain-separated dstack `GetKey` result is expanded with
  HKDF-SHA-256 and used as an AES-256-GCM wrapping key. Only the sealed PKCS#8
  document is stored in the `participant-key-state` named volume; the
  plaintext private and public PEM files exist only below `/run/vita-fl` on
  tmpfs. A corrupt or undecryptable state fails closed instead of silently
  rotating the participant identity.
- The participant public key is included in the worker's REPORTDATA-bound
  DCAP registration. This RSA key signs and decrypts model artifacts, and
  Worker 0's co-located inference process uses it to decrypt the model bundle.
  It is distinct from the secp256k1 action key that authorizes Ethereum
  transactions. The AIR evidence signing key remains a third,
  domain-separated dstack-derived key.
- Worker 0 exposes port 8080 from the same `dfl-worker` container and CVM.
  Model retrieval remains tool-triggered: container startup does not fetch or
  load the current model. The agent normally reads Worker 0's authorized,
  REPORTDATA-bound HTTPS endpoint from `DeviceRegistry.public_ip`; an explicit
  `tee_inference_url_override` is only a diagnostic or compatibility escape
  hatch. Tool-triggered model loading uses the same compose-bound GMStorage,
  Registry, RPC endpoint, and chain ID as the worker. A present contract
  manifest is validated, but a missing MFS copy does not disable inference
  after a valid Worker 0 reboot.
- Worker containers use `restart: unless-stopped`. After training completes,
  the supervisor exits once so Docker reboots the container. The rebooted
  worker derives the same app-bound action key, reuses its existing on-chain
  registration, and remains idle when the configured rounds are already
  complete. Worker 0 keeps its co-located TEE-inference service available in
  that idle process. This avoids both a restart loop and another DCAP
  registration. The normal completion path never calls `deregisterDevice`;
  deregistration remains an explicit participant action.
- `ssh_public_key_path` can still configure the contract runtime and auxiliary
  apps. Official static and dynamically launched worker resources always pass
  an empty SSH-key list and no user-defined pre-launch script.
- Non-secret worker configuration is rendered into Compose; secret values use the provider's encrypted app environment.
- The default minimal hardware profile is now `tdx.small` with `20 GB` disk.
- The contract-runtime TEE runs its own local `anvil`; the worker TEE talks to that internal runtime endpoint, not to Sepolia.
- The embedded Anvil runtime creates 500 funded deterministic accounts, matching
  the fixed W0--W499 prototype pool. Terraform additionally passes explicitly
  configured static worker addresses through `WORKER_ACCOUNT_ADDRESSES` so the
  bootstrap can fund non-default static identities before registration.
- The worker's measured `RPC_URL`, `EXPECTED_CHAIN_ID`, and `EXPECTED_*`
  contract addresses are the authoritative startup and recovery configuration.
  If `/runtime/admission-ready.json` or `/runtime/contracts.json` is present,
  the worker validates that optional first-deployment hand-off against those
  values. Missing mutable MFS metadata therefore cannot block a valid worker
  reboot; deployed bytecode and on-chain state remain authoritative.
- W0 additionally reads `/runtime/bootstrap-recipients.json` for the initial
  round-0 readiness hand-off, verifies it against the exact immutable on-chain
  run roster, and performs the encryption rollover. The other selected workers
  wait for round 1 before training.
- The worker image expects the real Phala attestation socket. In this scaffold the worker compose mounts `/var/run/dstack.sock` and keeps `/var/run/tappd.sock` only for compatibility.
- At runtime the worker requires `app_compose` from Phala `info()` and checks its SDK-compatible canonicalization against the live `compose-hash` event as a local consistency preflight. The authoritative decision is on-chain: the Registry derives both the image digest and role policy from the submitted byte preimage before calculating its complete Compose hash.
- The legacy `tappd.sock` path is not accepted unless it also exposes `app_compose`; otherwise the worker aborts instead of trusting a mock or env-only digest.
- After changing the worker attestation code, publish a fresh `ghcr.io/uzhw8rgl/master-thesis-dfl-worker:phala` image before redeploying the Phala workers, otherwise the running CVMs still use the old logic baked into the last image.

### Where Hardware Is Selected

The Phala hardware and placement are chosen through the following Terraform inputs:

- `size`: the machine class, for example `tdx.small`, `tdx.medium`, or a GPU family size
- `region`: deployment region such as `US-WEST-1`
- `os_image`: the Phala base image slug
- `disk_size`: attached storage size in GB
- `worker_replicas`: how many identical worker CVMs should run
- `node_id` (optional): pinning to one specific worker node

In this scaffold those values are wired into `resource "phala_app" "contract_runtime"` and `resource "phala_app" "dfl_worker"` in `main.tf`.

## Flow

1. Publish the combined DFL worker image through the GitHub Actions workflow
   `Publish DFL Worker Image`. On branches `phala`, `tee_inference`, and
   `phala_app_key`, changes to either the worker or embedded inference code
   trigger this workflow and keep the default `phala` tag.
2. Copy the digest-pinned worker image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:98ce74bcee923ca73dae9ae4780e91dc1e2ba6c12ef47973a76f5533cb24b3b4
```

3. Replace the image reference in `dstack-compose.template.yml` with the digest-pinned worker image.
4. Publish the runtime image through the manual GitHub Actions workflow `Publish Smart Contracts Image`.
5. Copy the digest-pinned runtime image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:6cc436a7ddeb0edb06708d0292529f48fcdc6df55c22a9c8f9238cb12c4063b4
```

6. Use that digest-pinned runtime image for `smart_contracts_image` in Terraform or in `dstack-compose.contracts.template.yml`.
7. Deploy the digest-pinned compose files on Phala/dstack.
8. Fetch a current TDX quote for PCCS collateral discovery and store it as
   `data/phala_tdx_quote`.
9. Keep the owner-reviewed base-runtime policy quote separately at
   `data/dstack-dev-0.5.9-de9c74f0-reference-tdx-quote`. Replace it only when
   intentionally approving a different dstack base-runtime image, then review
   its `MRTD` and `RTMR0`--`RTMR2` values and rebuild the smart-contract image.
10. Optionally inspect its RTMR3:

```bash
scripts/extract_tdx_rtmr3.py data/phala_tdx_quote
```

11. Start the local stack.

```bash
docker compose down --volumes --remove-orphans
docker compose up --build --force-recreate
```

During deployment, `starter_docker.sh` requires the digest-pinned
`EXPECTED_WORKER_IMAGE` and two Terraform-rendered policy-reference
app-composes. It derives their hashes with the Registry's canonical pure
function, requires distinct training-only and inference-role hashes, and
owner-provisions them before opening admission. Missing references abort Phala
startup rather than learning policy from the first worker. The local mock path
uses two deterministic, source-defined reference profiles instead.

For Phala/dstack, the measured `compose-hash` is not just the SHA-256 of `dstack-compose.template.yml`. In practice there are two related hashes:

- the RTMR3 `compose-hash` event: SHA-256 of the normalized/canonical `app_compose` byte preimage; `phala/app_code.txt` may contain an exported copy for offline inspection
- the raw compose-file hash: SHA-256 of the `docker_compose_file` text, which can match your local `dstack-compose.template.yml`

Compose hashes may differ between workers. The Registry first requires the
derived image digest and one owner-provisioned role policy. The policy covers
entrypoint, command, user, mounts and dstack-socket access, capabilities,
security options, networking, and environment safety. Known per-worker values
are normalized by environment-key name; fixed and unknown values are hashed.
Unknown service fields, duplicate environment keys, extra services, and
top-level bind-volume options fail closed. The contract then calculates
`SHA-256` over the complete submitted `app_compose` bytes. The verifier hashes
the structured event fields on-chain, requires that hash in the unique
`compose-hash` event, replays RTMR3, and compares it with the hardware-signed
quote.

Independently, bootstrap extracts `MRTD` and `RTMR0`--`RTMR2` from the owner-reviewed dstack reference quote and pins that OS/boot tuple in the verifier. The structured-log selector is fail-closed until this tuple is configured and requires every live worker quote to match it. This version-bound policy file is not the quote used for PCCS collateral discovery. The reference quote does not pin the worker-specific Compose hash or `RTMR3`; those remain live-derived and may differ between workers.

For an independent offline audit, a live worker can publish its current RTMR3 event log and app-code export into the runtime Kubo MFS under `/phala-artifacts/latest`. Pull those into the repository with:

```bash
scripts/fetch_phala_worker_artifacts.sh "$KUBO_API"
```

This refreshes `phala/rtmr3_event_log.txt` and `phala/app_code.txt` when Phala exposes them. These are audit copies, not bootstrap inputs, and their hashes are never provisioned as registration policy. Treat exports as deployment artifacts and do not add new exports to version control.

## Manual Runtime-Only Redeploy

If you only want to rebuild the contract-runtime TEE manually in the Phala UI, use:

- `phala/dstack-compose.contracts.runtime-only.yml`

That file is a fully rendered runtime compose for the current `phala` image set and current local DFL values.

Important caveat:

- the worker TEEs currently reference the runtime TEE by its concrete Phala endpoint URL for `KUBO_API`, `KUBO_GATEWAY`, and `RPC_URL`
- if the runtime app is recreated and gets a new endpoint, the workers must be updated to the new runtime endpoint before they can talk to Anvil/IPFS again
- this is the main reason a full Terraform apply currently wants to replace the workers too
- Phala can encode the exposed service port directly in the hostname, e.g. `https://<app>-5001.dstack-...`; worker wiring must replace that embedded port marker with `-8545`, `-5001`, and `-8080` rather than appending `:8545`, `:5001`, or `:8080`

So the safe manual order is:

1. Redeploy the runtime app with `dstack-compose.contracts.runtime-only.yml`.
2. Note the new runtime endpoint.
3. Update the worker app compose files so `KUBO_API`, `KUBO_GATEWAY`, and `RPC_URL` point at that new runtime endpoint.
4. Redeploy the workers only if the runtime endpoint changed.

## Worker Attestation Notes

The Phala worker path is now intentionally "real quote only":

- no fallback to `TDX_QUOTE_PATH`
- no mock quote registration path for Phala workers
- the worker must be able to talk to the Phala socket exposed inside the CVM

If the worker log shows errors like:

- `Unix socket file /var/run/dstack.sock does not exist`
- `write EPIPE`
- `failed to parse response`

then the first thing to verify is that the currently deployed worker image digest was built after the latest `dfl/node_server` attestation changes. Those errors usually mean the CVM still runs an older image that does not yet speak the socket/API variant exposed by Phala on that node.

For explicit overrides, `phala/tf-env.sh` supports:

- `PHALA_RUNTIME_ENDPOINT_OVERRIDE`
- `PHALA_RUNTIME_RPC_URL`
- `PHALA_RUNTIME_KUBO_API_URL`
- `PHALA_RUNTIME_KUBO_GATEWAY_URL`

If only `PHALA_RUNTIME_ENDPOINT_OVERRIDE` is set and it already contains an embedded Phala port hostname such as `...-5001.dstack-...`, Terraform now derives the matching `-8545`, `-5001`, and `-8080` hostnames automatically.

## What This Verifies

The on-chain path receives the quote, the exact canonical `app_compose` bytes, and the ordered RTMR3 event fields. It does not accept a worker-provided image identity, compose hash, or precomputed event digest as policy truth.

`DeviceRegistry` derives the Docker image digest and `workerPolicyHash`, then
requires both the configured image and one of the two pre-provisioned role
policies. It independently computes `SHA-256(app_compose)`. The attestation
contract requires the owner-pinned dstack `MRTD`/`RTMR0`--`RTMR2` base-runtime
tuple, validates each structured event, computes its SHA-384 digest on-chain,
requires exactly one matching `compose-hash` payload, replays the extend chain
from the zero RTMR3 value, and requires the result to equal RTMR3 inside the
verified quote. Registry-generated `REPORTDATA` binds the complete Compose
hash, image digest, role-policy hash, logical participant address, current
action address, endpoints, participant RSA public key, verifier, deployment,
and nonce. Registration additionally requires an EIP-712 enrollment signature
from the logical participant and the transaction itself must be sent by that
action address. Only an identity in the owner-committed run roster that also
satisfies this attestation and workload policy can register. Once every
committed identity is registered, the roster and its action-key/public-key
bindings are frozen for the run.

This means worker-specific `app-id`, `instance-id`, variable environment values,
and final RTMR3 measurements need not be known in advance. Security-relevant
Compose changes nevertheless produce a different, non-provisioned role policy,
while the complete variable Compose remains quote-bound.

The official launcher configuration removes worker SSH keys, but this is a
deployment control rather than an attestation claim: Phala supplies
`ssh_authorized_keys` through separate user configuration that is not part of
the currently verified `app_compose`. Consequently, roster admission cannot by
itself cryptographically prove that a committed participant's independently
launched, otherwise-identical CVM has no SSH key. A production claim that the
action key is non-exportable would
need an attested user-configuration policy, trusted deployment identity, or a
non-exporting signing interface in addition to the checks above.

## Next Steps

For the Phala layout with one contract-runtime TEE and a configurable number of
worker TEEs, the sequence is:

1. Deploy the contract-runtime TEE with `anvil`, `ipfs`, and `smart-contracts`.
2. In Training Setup, commit the exact ordered participant roster and then
   deploy those worker TEEs, all using the same digest-pinned combined worker
   image. Worker 0 also exposes TEE inference.
3. Optional: from a worker deployment, export measured artifacts for local consistency checks:
   - the TDX quote into `data/phala_tdx_quote`
   - the RTMR3 event log into `phala/rtmr3_event_log.txt`
   - the Phala app-code object into `phala/app_code.txt`
4. Optional: verify locally that the copied artifacts are internally consistent:

```bash
python scripts/verify_phala_rtmr3.py \
  phala/rtmr3_event_log.txt \
  --quote data/phala_tdx_quote \
  --compose phala/dstack-compose.template.yml \
  --app-code phala/app_code.txt
```

5. After changing the worker image or a security-relevant worker template field,
   update `worker_image` and redeploy the contract runtime so it installs the
   new image digest and both freshly derived role policies. Normalized
   per-worker values require no policy change.

Important:

- The on-chain attestation policy verifies the live worker quote and event replay, not the contract-runtime TEE quote.
- The dstack reference quote is mandatory for pinning `MRTD` and `RTMR0`--`RTMR2`, but it is not a Compose or `RTMR3` allowlist. It must be selected explicitly for the worker OS version; bootstrap aborts instead of falling back to the PCCS quote. `PHALA_ENFORCE_REFERENCE_RTMR3` remains a separate legacy-only debugging input. The new `registerDeviceWithAttestedAppCompose` selector calls the structured-log verifier without exact-`RTMR3` enforcement, so keep that flag disabled for the normal Phala flow.
- If the worker digest changes, update `worker_image` and redeploy the contract runtime so it installs the new expected digest. Rebuild the `smart-contracts` image when its contract/bootstrap code or either packaged quote artifact changes.

Current Terraform defaults in this scaffold match that target layout:

- `contracts_app_name = "master-thesis-contract-runtime-phala"`
- `worker_app_name = "master-thesis-dfl-worker-0"`
- `worker_replicas = 1`
- `contracts_size = "tdx.small"`
- `worker_size = "tdx.small"`
- `zk_inference_size = "tdx.medium"` (4 GB RAM; the ZK prover does not complete on the 2 GB `tdx.small` profile)
- `os_image = "dstack-dev-0.5.7"`
- `dynamic_worker_os_image = "dstack-dev-0.5.9"`

Use the canonical Phala OS selector above for dynamic workers. The
`de9c74f0` suffix belongs only to the separately reviewed reference-quote
artifact and is not part of the OS image slug returned by the Phala provider.

TEE inference has no independent image, enable flag, size, disk, or Phala app
setting. Publishing and selecting `worker_image` updates both Worker 0's DFL
process and its co-located inference process.

## Agent, Ollama, and SCITT

The browser agent runs inside the contract-runtime CVM so the authenticated UI
can reach it as `agent:8089`. Its SCITT-CCF transparency log runs alongside it
with a fresh tmpfs ledger on every container start. Ollama runs in a separate
`tdx.medium` CVM with 20 GB disk and pulls `qwen3:1.7b` during startup. Only the
Bearer-authenticated proxy is exposed through the Ollama app gateway; the raw
Ollama API is not published.

Publish the Agent and transparency-log images with the GitHub Actions workflows
`Publish Phala Agent` and `Publish Phala Transparency Log`, then place their
digest-pinned references in `.env.phala.anvil`:

```dotenv
ENABLE_PHALA_AGENT=true
AGENT_IMAGE=ghcr.io/uzhw8rgl/master-thesis-agent@sha256:337df28644a1db406cd34963cd0007e46266b3146333a911a6e3b76715ed96d3
TRANSPARENCY_LOG_IMAGE=ghcr.io/uzhw8rgl/master-thesis-transparency-log@sha256:4c6789921d5ff89e546c65c435bc19d479acc8248715dff3f5b1536e2c8af723
ENABLE_OLLAMA=true
OLLAMA_MODEL=qwen3:1.7b
OLLAMA_SIZE=tdx.medium
OLLAMA_API_TOKEN=replace-with-at-least-24-url-safe-characters
```

The agent itself does not wait for inference readiness: chat becomes ready as
soon as Ollama has loaded the model. For TEE tool calls it reads Worker 0's
current record from `DeviceRegistry` and uses the registered HTTPS endpoint.
Set `TEE_INFERENCE_URL_OVERRIDE` only to deliberately bypass that discovery for
diagnostics.

For receiver-attested confidential receipts on all six public MCP tools,
generate the Sello key material once and add its output to `.env.phala.anvil`:

```bash
python phala/generate_sello_env.py --scitt-url https://CONTRACT_APP_ID-8000s.dstack-REGION.phala.network
```

The trailing `s` selects dstack-gateway TLS passthrough, so Worker 0 and the
separate ZK-inference CVM connect directly to SCITT-CCF's own TLS listener
without an HTTP proxy.

With `ENABLE_SELLO_RECEIPTS=true`, Terraform gives each inference receiver only
its own signing seed and the token-issuer public key. The agent receives the
owner token/HPKE private material and a public registry for both receivers. Each
receiver registers its signed, owner-encrypted receipt directly with SCITT and
releases the tool response only after verifying the inclusion receipt. Set
`SELLO_SCITT_URL` to the contract-runtime app's public `-8000s` TLS-passthrough
URL so both inference receivers can reach SCITT-CCF directly.
