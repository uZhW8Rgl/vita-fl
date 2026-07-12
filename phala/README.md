# Phala/dstack Worker Image Attestation

This directory contains deployment templates for the contract runtime, DFL
workers, and the separately attested TEE inference service.

## Terraform Deployment

This directory also contains a Terraform scaffold for deploying the DFL prototype to Phala Cloud with the official provider `phala-network/phala` version `0.2.0-beta.3`.

Files:

- `main.tf`: provider, one `phala_app` for the contract runtime TEE, one `phala_app` for the worker TEE, optional `phala_ssh_key`, optional `phala_cvm_power`
- `variables.tf`: Phala and worker runtime configuration
- `outputs.tf`: deployed app metadata
- `terraform.tfvars.example`: values you can copy into `terraform.tfvars`
- `dstack-compose.contracts.phala.tftpl`: Terraform-rendered compose policy for the contract-runtime TEE
- `dstack-compose.worker.phala.tftpl`: Terraform-rendered compose policy for the worker TEE
- `dstack-compose.tee-inference.phala.tftpl`: Terraform-rendered compose policy for the TEE inference app
- `dstack-compose.contracts.template.yml`: manual compose policy for the contract-runtime TEE
- `dstack-compose.template.yml`: manual compose policy for the worker TEE

Typical workflow:

```bash
python3 scripts/prepare_dfl_worker_experiment.py --workers 2 --skip-compose --skip-env
bash phala/tf-env.sh init
bash phala/tf-env.sh plan -input=false
bash phala/tf-env.sh apply
```

If you already keep the deployment values in repository-root env files, you can use the helper wrapper instead of duplicating secrets into `terraform.tfvars`:

```bash
bash phala/tf-env.sh init
bash phala/tf-env.sh plan -input=false
bash phala/tf-env.sh apply
```

The wrapper prefers `.env.phala.anvil` by default, falls back to `.env` if needed, and can be pointed at a custom file with `PHALA_ENV_FILE=/path/to/file`.

The wrapper reads these values from the selected env file:

- `PHALA_CLOUD_API_KEY`
- `W0_ACCOUNT_ADDRESS`
- `W0_PRIVATE_KEY`
- optional `W0_RSA_PRIVATE_KEY_FILE` and `W0_RSA_PUBLIC_KEY_FILE` paths; generated files under `data/rsa_keys/` are used by default
- optional `ENABLE_TEE_INFERENCE` and `TEE_INFERENCE_IMAGE`

It also forwards the current Anvil/DFL profile settings into Terraform, including:

- `W1_ACCOUNT_ADDRESS`, `W1_PRIVATE_KEY`, `W2_ACCOUNT_ADDRESS`, `W2_PRIVATE_KEY`, ... when `WORKER_COUNT` is greater than `1`
- `INITIAL_GM_SIGNER_ADDRESS`
- `CLIENT_LIMIT`, `EPOCH`, `ROUND`
- `MODEL_SUBMISSION_DEADLINE_MS`, `GM_UPDATE_TIMEOUT_MS`, `GM_UPDATE_TIMEOUT_LOOPS`
- `AGGREGATION_UPDATE_ESTIMATE_MS`, `GM_UPDATE_POLL_MS`
- `DATASET_NAME`
- `PCCS_FMSPC`, `PCCS_FETCH`, `PCCS_TEE`, `P256_MODE`
- `DEPLOY_TDX_V4_DCAP`, `VERIFY_TDX_QUOTE_ONCHAIN`, `AGGREGATOR_TIMEOUT_REPORT_PERCENT`
- `TDX_REFERENCE_QUOTE_PATH` for the owner-reviewed dstack base-runtime quote; when empty, bootstrap falls back to `PCCS_QUOTE_PATH`

This means a Phala deployment can now be driven directly from `.env.phala.anvil` without first rebuilding a combined `.env`.

For a direct Terraform invocation, keep secret values in `TF_VAR_*` process
environment variables and only non-secret paths in `terraform.tfvars`:

```bash
export TF_VAR_private_key="..."
terraform -chdir=phala plan
```

Notes:

- The provider's `env` attribute encrypts wallet and RSA key material for the target Phala app. The measured/public Compose contains only environment-variable names, never their values.
- Terraform still records sensitive `env` inputs in state. Local state, state backups, `terraform.tfvars`, and exported `app_code.txt` are ignored; use an encrypted, access-controlled remote backend for non-demo deployments.
- `public_logs` defaults to `true` for this observable Anvil demo deployment. Do not log secrets when adapting it for production.
- Worker and smart-contract images contain no private keys. Local Compose mounts generated development keys read-only; run `scripts/prepare_dfl_worker_experiment.py` first on a fresh clone.
- Keys that were committed previously must be treated as compromised and rotated outside this source change before production use.
- `worker_image` must stay pinned to a `sha256` digest. Its digest is the shared on-chain workload-policy identity; the worker-specific Compose hash is not an allowlist key.
- `smart_contracts_image` should also be pinned to a `sha256` digest when you want the contract-runtime TEE to be reproducible.
- The Terraform scaffold now separates `smart-contracts` and `dfl-worker` into different Phala apps / TEEs.
- The optional third `tee_inference` app runs in a separate CVM. Phala injects
  W0's account and RSA credentials through that app's encrypted environment.
  The service checks the RSA key against W0's authorized DeviceRegistry entry,
  fetches the current encrypted GMStorage/IPFS bundle, decrypts it inside the
  inference TEE, and verifies the authorized aggregator signatures before
  loading the native model.
- The inference app reads W0's registration but never registers W0 again, so it
  cannot overwrite Worker 0's DeviceRegistry record.
- If you want SSH access, set `ssh_public_key_path`; if you also want the key stored account-wide in Phala Cloud, set `manage_account_ssh_key = true`.
- Non-secret worker configuration is rendered into Compose; secret values use the provider's encrypted app environment.
- The default minimal hardware profile is now `tdx.small` with `20 GB` disk.
- The contract-runtime TEE runs its own local `anvil`; the worker TEE talks to that internal runtime endpoint, not to Sepolia.
- The contract-runtime compose receives `WORKER_ACCOUNT_ADDRESSES` from Terraform and the `smart-contracts` bootstrap funds those accounts on the embedded Anvil before workers register. This keeps `.env.phala.anvil` worker keys usable even when they are not part of Anvil's initially funded account list.
- The worker resolves `REGISTRY_ADDRESS`, `AGGREGATOR_ADDRESS`, and `GM_STORAGE_ADDRESS` from the contract-runtime TEE's Kubo manifest at `/runtime/contracts.json`.
- The worker now treats `/runtime/contracts.json` as the effective runtime-ready signal. The runtime publishes that manifest only after `smart-contracts` finished its bootstrap path, which avoids startup races even when `/runtime/ready.json` is missing on Phala.
- The worker image expects the real Phala attestation socket. In this scaffold the worker compose mounts `/var/run/dstack.sock` and keeps `/var/run/tappd.sock` only for compatibility.
- At runtime the worker requires `app_compose` from Phala `info()` and checks its SDK-compatible canonicalization against the live `compose-hash` event as a local consistency preflight. The authoritative policy decision is on-chain: the Registry parses the image digest from the submitted byte preimage before deriving its compose hash.
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

1. Publish the DFL worker image through the GitHub Actions workflow `Publish DFL Worker Image`.
   On branch `phala`, pushes that touch `dfl/**` trigger the workflow automatically and keep the default `phala` tag.
2. Copy the digest-pinned worker image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:6ea849fd7c494829e258dea99a4aba7aa4095996cdb67f66b4ccf47d2120d0e3
```

3. Replace the image reference in `dstack-compose.template.yml` with the digest-pinned worker image.
4. Publish the runtime image through the manual GitHub Actions workflow `Publish Smart Contracts Image`.
5. Copy the digest-pinned runtime image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:5b6bafac3bd026632f4f53fe42ca2804b37849b231e34c5303d810b9433d0533
```

6. Use that digest-pinned runtime image for `smart_contracts_image` in Terraform or in `dstack-compose.contracts.template.yml`.
7. Deploy the digest-pinned compose files on Phala/dstack.
8. Fetch the TDX quote emitted by the worker.
9. Store the quote as `data/phala_tdx_quote`.
10. Optionally inspect its RTMR3:

```bash
scripts/extract_tdx_rtmr3.py data/phala_tdx_quote
```

11. Start the local stack.

```bash
docker compose down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose up --build --force-recreate
```

During deployment, `starter_docker.sh` requires the digest-pinned `EXPECTED_WORKER_IMAGE` input and configures its SHA-256 value as the single expected worker-image policy. It no longer derives policy from a local Compose file. During registration the worker submits the exact SDK-canonical `app_compose` byte preimage; `DeviceRegistry` first parses `docker_compose_file` and derives the single `services.dfl-worker.image` digest on-chain instead of trusting a worker-supplied digest.

For Phala/dstack, the measured `compose-hash` is not just the SHA-256 of `dstack-compose.template.yml`. In practice there are two related hashes:

- the RTMR3 `compose-hash` event: SHA-256 of the normalized/canonical `app_compose` byte preimage; `phala/app_code.txt` may contain an exported copy for offline inspection
- the raw compose-file hash: SHA-256 of the `docker_compose_file` text, which can match your local `dstack-compose.template.yml`

Compose hashes may differ between workers. Only after the derived image digest matches the configured policy does the contract calculate `SHA-256` over the submitted `app_compose` bytes. The worker submits ordered structured event fields `(eventType, eventName, eventPayload)`, not trusted precomputed digests. The verifier hashes those fields on-chain with dstack's SHA-384 event serialization, requires the derived hash in the unique `compose-hash` event, replays RTMR3, and compares it with the hardware-signed quote. No compose-hash allowlist is used.

Independently, bootstrap extracts `MRTD` and `RTMR0`--`RTMR2` from the owner-reviewed dstack reference quote and pins that OS/boot tuple in the verifier. The structured-log selector is fail-closed until this tuple is configured and requires every live worker quote to match it. The reference quote does not pin the worker-specific Compose hash or `RTMR3`; those remain live-derived and may differ between workers.

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

- the worker TEEs currently reference the runtime TEE by its concrete Phala endpoint URL for `KUBO_API`, `KUBO_GATEWAY`, and `SEPOLIA_RPC_URL`
- if the runtime app is recreated and gets a new endpoint, the workers must be updated to the new runtime endpoint before they can talk to Anvil/IPFS again
- this is the main reason a full Terraform apply currently wants to replace the workers too
- Phala can encode the exposed service port directly in the hostname, e.g. `https://<app>-5001.dstack-...`; worker wiring must replace that embedded port marker with `-8545`, `-5001`, and `-8080` rather than appending `:8545`, `:5001`, or `:8080`

So the safe manual order is:

1. Redeploy the runtime app with `dstack-compose.contracts.runtime-only.yml`.
2. Note the new runtime endpoint.
3. Update the worker app compose files so `KUBO_API`, `KUBO_GATEWAY`, and `SEPOLIA_RPC_URL` point at that new runtime endpoint.
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

`DeviceRegistry` strictly derives the Docker image digest first and compares it with `expectedWorkerImageDigest`. Its narrow parser also rejects a second service declaration and YAML flow- or merge-style service declarations. It then computes `SHA-256(app_compose)`. The attestation contract requires the owner-pinned dstack `MRTD`/`RTMR0`--`RTMR2` base-runtime tuple, validates each structured event type, name and payload, computes its SHA-384 digest on-chain, requires exactly one matching `compose-hash` payload, replays the extend chain from the zero RTMR3 value, and requires the result to equal RTMR3 inside the verified quote. The Registry-generated `REPORTDATA` additionally binds the derived image/compose identity to the device address, endpoint metadata, RSA public key, deployment, owner challenge and nonce.

This means worker-specific `app-id`, `instance-id`, and compose measurements may still produce different final RTMR3 values, but those final RTMR3 values no longer need to be known in advance. The shared policy anchor is the digest-pinned worker image reference measured inside each worker's Phala app-compose preimage.

## Next Steps

For the planned Phala layout with one contract-runtime TEE and three worker TEEs, the next practical sequence is:

1. Deploy the contract-runtime TEE with `anvil`, `ipfs`, and `smart-contracts`.
2. Deploy the three worker TEEs, all using the digest-pinned worker image.
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

5. After changing the worker image, update `worker_image` and redeploy the contract-runtime so it installs the new expected image digest. Ordinary per-worker Compose differences require no allowlist update.

Important:

- The on-chain attestation policy verifies the live worker quote and event replay, not the contract-runtime TEE quote.
- The dstack reference quote is mandatory for pinning `MRTD` and `RTMR0`--`RTMR2`, but it is not a Compose or `RTMR3` allowlist. `PHALA_ENFORCE_REFERENCE_RTMR3` remains a separate legacy-only debugging input. The new `registerDeviceWithAttestedAppCompose` selector calls the structured-log verifier without exact-`RTMR3` enforcement, so keep that flag disabled for the normal Phala flow.
- If the worker digest changes, update `worker_image` and redeploy the contract runtime so it installs the new expected digest. Rebuild the `smart-contracts` image only when its contract or bootstrap code changes.

Current Terraform defaults in this scaffold match that target layout:

- `contracts_app_name = "master-thesis-contract-runtime-phala"`
- `worker_app_name = "master-thesis-dfl-worker-0"`
- `worker_replicas = 1`
- `contracts_size = "tdx.small"`
- `worker_size = "tdx.small"`
- `os_image = "dstack-dev-0.5.7"`
- `tee_inference_image = "ghcr.io/uzhw8rgl/master-thesis-tee-inference@sha256:6ce20ad296c57b912b711479c71d5b2115c299ba7fe5a33b387a5c826d6ef880"`
- `enable_tee_inference = false` until the updated image has been published

To enable the separate third app:

```bash
export ENABLE_TEE_INFERENCE=true
export TEE_INFERENCE_IMAGE=ghcr.io/uzhw8rgl/master-thesis-tee-inference@sha256:6ce20ad296c57b912b711479c71d5b2115c299ba7fe5a33b387a5c826d6ef880
bash phala/tf-env.sh plan -input=false
bash phala/tf-env.sh apply -input=false -auto-approve
```
