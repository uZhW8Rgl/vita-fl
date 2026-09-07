# Phala Deployment and Worker Image Attestation

This directory contains deployment templates for the contract runtime and the
DFL worker TEEs. Worker 0 additionally serves TEE inference from the same
digest-pinned `dfl-worker` image and the same CVM; there is no separate
TEE-inference Phala app.

## Terraform Deployment

The supported entry point is `bash phala/start.sh`, run from the repository root.
It deploys the DFL prototype through Terraform and the official provider
`phala-network/phala` version `0.2.0-beta.3`, resolves image tags to digests, and
handles existing workers before a deployment change.

Files:

- `main.tf`: provider, one `phala_app` for the contract runtime TEE, worker apps, optional `phala_ssh_key`, and optional `phala_cvm_power`
- `variables.tf`: Phala and worker runtime configuration
- `outputs.tf`: deployed app metadata and the selected image set/source revisions
- `start.sh` / `deploy.py`: startup, image updates, recovery, and teardown
- `github_state.py`: encrypted GitHub state snapshots and deployment locking
- `tf-env.sh`: validated transport of env configuration into Terraform
- `image-sources.json` / `resolve_images.py`: release tags and digest resolution
- `terraform.tfvars.example`: reference for optional Terraform settings
- `dstack-compose.contracts.phala.tftpl`: Terraform-rendered compose policy for the contract-runtime TEE
- `dstack-compose.worker.phala.tftpl`: Terraform-rendered compose policy for a
  worker TEE; its Worker 0 rendering also enables the co-located inference
  process
- `dstack-compose.contracts.template.yml`: manual compose policy for the contract-runtime TEE
- `dstack-compose.template.yml`: manual Worker 0 compose policy, including the
  co-located inference process

### One-time setup

Shared Terraform state is stored **GPG-encrypted in the `phala-state` branch of
this GitHub repository**. No additional cloud account, storage service, or Phala
VM is needed. GitHub Actions still uses the repository's normal minutes and
storage quota. Run these steps from the repository root with Python 3, GPG,
GitHub CLI, and Terraform available; the bundled Terraform 1.9.8 is sufficient.

1. Keep all Phala settings in `.env.phala.anvil`, including
   `PHALA_CLOUD_API_KEY`, UI credentials, and the complete W0--W499 inventory.
   When migrating an older setup, copy any required settings from `.env.shared`
   into this profile once; Phala commands do not read `.env.shared`.
   Only if the profile does not exist, copy
   `.env.phala.anvil.example` and fill in its placeholders. Add:

   ```dotenv
   PHALA_STATE_REPOSITORY=uZhW8Rgl/vita-fl
   PHALA_STATE_PASSPHRASE=YOUR-LONG-RANDOM-PASSWORD
   ```

   Generate a password with at least 32 characters using your password manager or
   `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` and save it
   privately. You will also use it as a GitHub secret in step 4.
2. Sign in to GitHub with repository write access. If a local deployment
   exists, privately back up `phala/terraform.tfstate` first. Initialize the
   shared state once, then check the planned deployment:

   ```bash
   gh auth login
   bash phala/start.sh --init-github-state --init-only
   bash phala/start.sh --dry-run
   ```

   Initialization uploads the existing local state, or creates an empty state
   for a new deployment, without starting or deleting any Phala apps. Keep
   automatic deployment disabled until setup is complete.
3. Encrypt the completed env file, choosing a separate strong passphrase:

   ```bash
   gpg --symmetric --cipher-algo AES256 --output phala/deploy.env.gpg .env.phala.anvil
   ```

   Commit only the encrypted file. Repeat encryption whenever deployment
   settings change, using the same env passphrase. An env change does not require
   reinitializing the shared Terraform state or recreating the GitHub environment.
   The full inventory exceeds GitHub's single-secret size
   limit; see [GitHub's large-secret procedure](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets#storing-large-secrets).
4. In GitHub **Settings → Environments**, create `phala` for deployment branch
   `phala_app_key`. Add the secrets `PHALA_STATE_PASSPHRASE` from step 1 and
   `PHALA_ENV_PASSPHRASE` from step 3. These are the only two required deployment
   secrets; the Phala API key is inside the encrypted env file. Leave required reviewers disabled
   for automatic deployment.
5. In **Settings → Secrets and variables → Actions → Variables**, set
   `PHALA_AUTO_DEPLOY=true` and `PHALA_DEPLOY_BRANCH=phala_app_key`. Commit and
   push the implementation and `phala/deploy.env.gpg` to that branch. A change
   to this encrypted file also triggers the UI build and deployment.
6. Wait for all builds and deploy jobs to finish. Open the reported UI, choose
   **Training Setup**, and press **Start Training** when you want training to
   begin.

After setup, local startup or updates use one command:

```bash
bash phala/start.sh
```

The launcher reads the GitHub state settings from your Phala env profile.
Locally it uses `gh auth token`, or `PHALA_STATE_TOKEN`/`GH_TOKEN` when supplied
in the shell; a fine-grained token needs repository **Contents: read and write**.
CI uses its existing `GITHUB_TOKEN` and needs no personal access token secret.
Without GitHub state settings, the launcher retains the local-only state mode
in `phala/terraform.tfstate`.

Local Phala commands and GitHub Actions use one complete env profile.
Use `PHALA_ENV_FILE=/absolute/path/to/config` for another profile.
Terraform can be on `PATH`, in `phala/bin/terraform`, or selected with
`TERRAFORM_BIN=/absolute/path/to/terraform`. Image resolution uses Python's
standard library and needs access to GHCR and Phala, without local Docker.
Phala must be able to pull the published images. The launcher resolves release
tags from `image-sources.json`; old `*_IMAGE` pins do not prevent updates.

Terraform startup prepares the runtime and its current RPC/Kubo endpoints.
The UI commits the participant roster and launches the worker TEEs. A successful
Terraform apply confirms resource creation; application bootstrap and model
loading can still be in progress when the CVMs become ready.

### Automatic deployment after image publishing

[deploy-phala.yml](../.github/workflows/deploy-phala.yml) is called by the seven
Phala image publish workflows after their build and registry push succeed:
DFL worker, smart contracts, Control API, UI, agent, transparency log, and ZK
inference. Each call passes the exact built digest, preserves the other image
pins from shared Terraform state, and invokes the same launcher used locally.
The standalone TEE-inference publisher is excluded because Worker 0 uses the
combined DFL worker image. The separate ZK app remains disabled by the current
Terraform policy; tracking its image does not enable that app.

The [one-time setup](#one-time-setup) configures the required values. Available
settings are:

| Kind | Name | Value |
| --- | --- | --- |
| Environment secret | `PHALA_STATE_PASSPHRASE` | Password for the encrypted shared Terraform state; match the local Phala profile. |
| Environment secret | `PHALA_ENV_PASSPHRASE` | Passphrase for `phala/deploy.env.gpg`; recommended for the complete inventory. |
| Environment secret | `PHALA_ENV_CONTENT` | Alternative plain env contents only for a complete configuration smaller than 48 KB; set this or the env passphrase, never both. |
| Repository variable | `PHALA_AUTO_DEPLOY` | `true` enables deployment after successful publication. |
| Repository variable | `PHALA_DEPLOY_BRANCH` | Optional branch name; defaults to `phala_app_key`. |
| Repository variable | `PHALA_STATE_BRANCH` | Optional shared-state branch; defaults to `phala-state`. Match `PHALA_STATE_BRANCH` in your local profile when changing it. |
| Repository variable | `PHALA_ENV_ENCRYPTED_FILE` | Optional repository-relative encrypted env path; defaults to `phala/deploy.env.gpg`. |

Deployment workflows request `contents: write` to update encrypted state and the
deployment lock in the same repository. Keep the state branch outside any rule
that would prevent those writes. The default state branch is `phala-state`;
local commands and CI must use the same repository, branch, and passphrase.

If you choose another deployment branch, also add it to the `push.branches`
lists of the image publishers; the variable selects deployment eligibility and
does not change their build triggers. The default `phala_app_key` branch is
already covered by all seven workflows. Deployment uses the current branch's
Terraform/configuration code while retaining the exact image digest and source
revision from the successful build.

The workflows pass secrets through a reusable workflow whose deployment job
references the `phala` environment. Environment protection rules therefore also
apply to these deployments; a required reviewer makes the job wait for approval.
See [GitHub reusable workflow secrets](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#using-inputs-and-secrets-in-a-reusable-workflow).

Deployment jobs share the `phala-deploy` concurrency group with `queue: max` and
`cancel-in-progress: false`. Up to 100 pending jobs wait while the active job
finishes; jobs beyond that GitHub limit are canceled and need to be rerun. See
[GitHub concurrency queues](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency#example-queueing-multiple-pending-runs).
An exclusive GitHub deployment lock also coordinates local commands and CI for
the whole deployment. Older builds of a component are skipped if a
newer descendant revision is already recorded; divergent revisions fail for
review. Failed builds never reach deployment. Terraform failures fail the deploy
job and retain the state for a retry; they do not report a successful rollout or
restore an already destroyed training run.

Before changing or deleting cloud apps, Terraform records the desired images
and source revisions in an independent `deployment_manifest` state resource.
A reset preserves that record, so CI retries retain the intended image set even
if a previous apply failed after deleting the runtime. Explicit `--destroy`
removes the deployment manifest as well.

When one source update publishes several components, wait for all queued
deployment jobs to finish before starting the next training run.

### Existing workers, retries, and recovery

This is an ephemeral training prototype. **A material runtime/worker app change or
`--recreate` destroys the current deployment and its worker CVMs before creating
a fresh deployment. Anvil state, IPFS artifacts, training progress, and runtime
evaluation data are lost.** Export an experiment's evaluation data before
publishing deployment changes. After the update, start a new run from the UI.

| Situation | Launcher behavior |
| --- | --- |
| Fresh account and state | Creates the runtime and configured auxiliary apps, then wires their current endpoints. Workers start through the UI. |
| Same images and app configuration | Leaves the existing deployment and workers running. |
| Changed runtime/worker image or measured app configuration | Removes existing workers and recreates the demo so the on-chain roster and image policy agree. |
| Auxiliary app change or image metadata for a disabled service | Applies the Terraform change without resetting a healthy runtime and worker roster. |
| Stopped workers or leftover workers outside root Terraform state | Checks Phala's app inventory and removes matching deployment workers during reset or teardown. |
| Old runtime exists without local/shared state | Reports and reconciles reserved deployment apps through the reset path; restore/migrate the original state first when preserving ownership is intended. |
| Interrupted deployment or a resource manually deleted in Phala | Refreshes the plan and cloud inventory on the next run and reconciles the remaining apps. |
| Registry, credentials, inventory, or planning failure | Aborts before the destructive phase; fix the reported problem and rerun. |
| Deletion fails or an old app is still present | Stops the rollout; rerun after resolving the Phala error. |

Cleanup is limited to app IDs held by this Terraform state, configured app
names, and the reserved prototype names (including numbered W0--W499 workers
and supported legacy worker names). Use one deployment per naming scope/account
workspace. Apps with these reserved names are treated as belonging to this
prototype, even if they were created manually. Unrelated names are preserved.

Preview the plan and matching cleanup candidates, explicitly recreate the demo,
or remove it with your configured deployment profile:

```bash
bash phala/start.sh --dry-run
bash phala/start.sh --recreate
bash phala/start.sh --destroy
```

`--dry-run` reads registry metadata, Terraform state, and the Phala inventory and
initializes local Terraform files, but does not apply or delete apps. `--destroy`
also cleans matching workers whose ownership state was held inside the old
Control API volume. Keep using the same GitHub state repository, branch, and
passphrase for all local commands and CI runs. The deployment lock prevents a
local command and CI from changing the deployment at the same time.

Initialize an existing local deployment with
`bash phala/start.sh --init-github-state --init-only` before enabling CI. Back up
the local state privately first; initialization does not replace an existing
shared state. Do not initialize another independent state for apps already
managed by this deployment.

State snapshots are encrypted before upload to `terraform.tfstate.gpg` and
retained in the state branch's Git history. Keep the state passphrase in a
password manager: neither GitHub nor Phala can recover it. Terraform state
contains sensitive deployment values;
plaintext state, plans, and env files remain excluded from Git.

If a process is interrupted, its GitHub lock can remain in place. First check
that its local process or CI job has stopped. If its latest state was already
uploaded, release the exact lock ID reported by the launcher:

```bash
bash phala/start.sh --unlock-github-state LOCK_ID
```

The launcher saves state after Terraform operations, including failed applies.
If an upload fails, the lock remains held and the launcher preserves the latest
encrypted snapshot as `phala/state-recovery.gpg`; CI also uploads this file as a
recovery artifact. Download it and recover with the same lock ID:

```bash
bash phala/start.sh --recover-github-state /path/to/state-recovery.gpg --github-state-lock-id LOCK_ID
```

The recovery command also accepts a private, unencrypted `terraform.tfstate`.
It validates the state, refuses to overwrite newer state, uploads the encrypted
snapshot, and releases the lock after success. Then rerun the original command.
Encrypted snapshots in the state branch's history and private backups provide
additional recovery copies; resetting orphan apps cannot recover lost training
data.

Use `--pinned` only for a deliberate deployment with explicit image pins: all
seven application `*_IMAGE` values must be complete GHCR digest references in
the env profile. The normal local startup path resolves current release tags
automatically. The durable Terraform outputs `deployment_images` and
`deployment_image_revisions` record the selected image set and available source
revisions, including interrupted deployments. These outputs identify deployment
intent; check the workflow result and application UI to confirm successful
startup.

### Application startup and training lifecycle

The application bootstrap is separated from deployment:

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
data as a ZIP archive of CSV tables. In Phala mode, non-transaction runtime
events remain signed in-memory telemetry, while every accepted receipt-derived
worker transaction is synchronously persisted in the shared evaluation volume
and de-duplicated by transaction hash. A new run clears that store before, not
after, worker deployment. RTMR3 registration uses its own operation label, so
Grafana exposes registration gas for every worker and a receipt-gap indicator
that must remain zero against the configured run roster. The dashboards and
evaluation reports surface gas as the sole resource accounting unit. Raw
receipt fields remain in the export for auditability but are not presented as
public-network prices.

For low-level Terraform inspection, `tf-env.sh` loads the same env configuration
and validates the worker inventory:

```bash
bash phala/tf-env.sh init
bash phala/tf-env.sh plan -input=false
```

Use `start.sh` for deployment changes so image resolution, cloud cleanup, and
endpoint wiring run together. Raw `tf-env.sh apply` bypasses that lifecycle.

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

The launcher uses this wrapper to validate and transport the complete credential
inventory. Do not maintain a second copy of worker identities in
`terraform.tfvars`.

Notes:

- The provider's `env` attribute encrypts Ethereum wallet and component
  secrets for the target Phala app. The measured/public Compose contains only
  environment-variable names, never their values.
- The Control API receives the same encrypted Anvil-owner key as the contract
  deployer, plus only the internal `http://anvil:8545` write endpoint. Dynamic
  worker CVMs continue to receive the externally exposed restricted RPC URL.
- Terraform still records sensitive `env` inputs in state. Local state, state backups, `terraform.tfvars`, and exported `app_code.txt` are ignored. The shared state branch contains encrypted snapshots; protect its passphrase and repository access.
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

## Attestation policy and quote maintenance

Publishing a combined worker image updates its immutable deployment reference
automatically when CI deployment is enabled. The launcher also refreshes the
contract runtime so it installs the expected worker image and role policies.
The manually rendered Compose examples are reference material; the supported
launcher renders its deployment from the Terraform templates.

Keep `data/phala_tdx_quote` available for PCCS collateral discovery. Keep the
owner-reviewed base-runtime policy quote separately at
`data/dstack-dev-0.5.9-de9c74f0-reference-tdx-quote`. Replace the policy quote only
when intentionally approving another dstack base-runtime image, review its
`MRTD` and `RTMR0`--`RTMR2` values, and rebuild the smart-contract image. An
application image update does not implicitly approve another base OS.

Optionally inspect the collateral-discovery quote's RTMR3:

```bash
scripts/extract_tdx_rtmr3.py data/phala_tdx_quote
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

## Runtime endpoint changes

The launcher derives RPC, Kubo API, Kubo gateway, and the default SCITT endpoint
from the newly deployed runtime. It disregards obsolete endpoint overrides in
old env files for its managed deployment path. Phala encodes exposed ports in
hostnames, for example `https://<app>-5001.dstack-...`; port-specific URLs need
`-8545`, `-5001`, and `-8080` in that hostname.

The historical `dstack-compose.contracts.runtime-only.yml` is a manually rendered
snapshot. It does not refresh image pins or manage worker ownership. Use
`start.sh --recreate` with the configured shared state to recreate the runtime and workers
with consistent endpoints and attestation policy.

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
   publish the updated component. Automatic deployment (or `start.sh` locally)
   installs the new image digest and freshly derived role policies. Normalized
   per-worker values require no policy change.

Important:

- The on-chain attestation policy verifies the live worker quote and event replay, not the contract-runtime TEE quote.
- The dstack reference quote is mandatory for pinning `MRTD` and `RTMR0`--`RTMR2`, but it is not a Compose or `RTMR3` allowlist. It must be selected explicitly for the worker OS version; bootstrap aborts instead of falling back to the PCCS quote. `PHALA_ENFORCE_REFERENCE_RTMR3` remains a separate legacy-only debugging input. The new `registerDeviceWithAttestedAppCompose` selector calls the structured-log verifier without exact-`RTMR3` enforcement, so keep that flag disabled for the normal Phala flow.
- Worker image updates require fresh contracts and a fresh run; the launcher handles that reset. Rebuild the `smart-contracts` image when its contract/bootstrap code or either packaged quote artifact changes.

Current Terraform defaults in this scaffold match that target layout:

- `contracts_app_name = "master-thesis-contract-runtime-phala"`
- `worker_app_name = "master-thesis-dfl-worker-0"`
- `worker_replicas = 1`
- `contracts_size = "tdx.small"`
- `worker_size = "tdx.small"`
- `enable_zk_inference = false` (required until separate ZK inference supports attested participant-key delegation)
- `zk_inference_size = "tdx.medium"` (legacy separate-app setting; inactive while ZK inference is disabled)
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

The `Publish Phala Agent` and `Publish Phala Transparency Log` workflows pass
their image digests to automatic deployment. Configure the services in
`.env.phala.anvil`; image references are resolved by the launcher:

```dotenv
ENABLE_PHALA_AGENT=true
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
generate the owner, issuer, and ZK-receiver key material once and add its
output to `.env.phala.anvil`:

```bash
python phala/generate_sello_env.py --scitt-url https://CONTRACT_APP_ID-8000s.dstack-REGION.phala.network
```

The trailing `s` selects dstack-gateway TLS passthrough, so Worker 0 connects
directly to SCITT-CCF's own TLS listener without an HTTP proxy. The launcher
derives the managed runtime's SCITT endpoint on deployment.

The legacy separate ZK receiver configuration uses an operator-generated signing
seed and the owner's static service registry, but that Phala app is currently
disabled pending attested participant-key delegation. With
`ENABLE_SELLO_RECEIPTS=true`, Worker 0 does not receive a TEE-receiver seed. Its
measured compose fixes `SELLO_SERVICE_KEY_PROVIDER=dstack`, and the receiver derives its
domain-separated Ed25519 key inside the active dstack CVM. The token-issuer
public key is the only Sello key material provisioned to Worker 0. The agent
resolves the TEE receiver key through Worker 0's attested DeviceRegistry
enrollment rather than through `SELLO_SERVICE_REGISTRY`.

Each receiver registers its signed, owner-encrypted receipt directly with SCITT
and releases the tool response only after verifying the inclusion receipt. For
manual Compose deployments, set `SELLO_SCITT_URL` to the contract-runtime app's
public `-8000s` TLS-passthrough URL. The managed launcher derives that URL.
