# Phala/dstack Worker Image Attestation

This directory contains the deployment template for generating a Phala/dstack TDX quote for the DFL worker image.

## Terraform Deployment

This directory now also contains a Terraform scaffold for deploying the DFL prototype to Phala Cloud with the official provider `phala-network/phala` (`0.2.0-beta.1` as documented on June 30, 2026).

Files:

- `main.tf`: provider, one `phala_app` for the contract runtime TEE, one `phala_app` for the worker TEE, optional `phala_ssh_key`, optional `phala_cvm_power`
- `variables.tf`: Phala and worker runtime configuration
- `outputs.tf`: deployed app metadata
- `terraform.tfvars.example`: values you can copy into `terraform.tfvars`
- `dstack-compose.contracts.phala.tftpl`: Terraform-rendered compose policy for the contract-runtime TEE
- `dstack-compose.worker.phala.tftpl`: Terraform-rendered compose policy for the worker TEE
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

It also forwards the current Anvil/DFL profile settings into Terraform, including:

- `W1_ACCOUNT_ADDRESS`, `W1_PRIVATE_KEY`, `W2_ACCOUNT_ADDRESS`, `W2_PRIVATE_KEY`, ... when `WORKER_COUNT` is greater than `1`
- `INITIAL_GM_SIGNER_ADDRESS`
- `CLIENT_LIMIT`, `EPOCH`, `ROUND`
- `MODEL_SUBMISSION_DEADLINE_MS`, `GM_UPDATE_TIMEOUT_MS`, `GM_UPDATE_TIMEOUT_LOOPS`
- `AGGREGATION_UPDATE_ESTIMATE_MS`, `GM_UPDATE_POLL_MS`
- `DATASET_NAME`
- `PCCS_FMSPC`, `PCCS_FETCH`, `PCCS_TEE`, `P256_MODE`
- `DEPLOY_TDX_V4_DCAP`, `VERIFY_TDX_QUOTE_ONCHAIN`, `AGGREGATOR_TIMEOUT_REPORT_PERCENT`

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
- `public_logs` defaults to `false`. Enabling it does not make secret logging safe.
- Worker and smart-contract images contain no private keys. Local Compose mounts generated development keys read-only; run `scripts/prepare_dfl_worker_experiment.py` first on a fresh clone.
- Keys that were committed previously must be treated as compromised and rotated outside this source change before production use.
- `worker_image` should stay pinned to a `sha256` digest to preserve a stable measured compose policy.
- `smart_contracts_image` should also be pinned to a `sha256` digest when you want the contract-runtime TEE to be reproducible.
- The Terraform scaffold now separates `smart-contracts` and `dfl-worker` into different Phala apps / TEEs.
- If you want SSH access, set `ssh_public_key_path`; if you also want the key stored account-wide in Phala Cloud, set `manage_account_ssh_key = true`.
- Non-secret worker configuration is rendered into Compose; secret values use the provider's encrypted app environment.
- The default minimal hardware profile is now `tdx.small` with `20 GB` disk.
- The contract-runtime TEE runs its own local `anvil`; the worker TEE talks to that internal runtime endpoint, not to Sepolia.
- The contract-runtime compose receives `WORKER_ACCOUNT_ADDRESSES` from Terraform and the `smart-contracts` bootstrap funds those accounts on the embedded Anvil before workers register. This keeps `.env.phala.anvil` worker keys usable even when they are not part of Anvil's initially funded account list.
- The worker resolves `REGISTRY_ADDRESS`, `AGGREGATOR_ADDRESS`, and `GM_STORAGE_ADDRESS` from the contract-runtime TEE's Kubo manifest at `/runtime/contracts.json`.
- The worker now treats `/runtime/contracts.json` as the effective runtime-ready signal. The runtime publishes that manifest only after `smart-contracts` finished its bootstrap path, which avoids startup races even when `/runtime/ready.json` is missing on Phala.
- The worker image expects the real Phala attestation socket. In this scaffold the worker compose mounts `/var/run/dstack.sock` and keeps `/var/run/tappd.sock` only for compatibility.
- At runtime the worker requires a measured `app_compose` from Phala `info()`, recomputes the normalized compose hash, compares it with the live quote's `compose-hash` event payload, and verifies that the measured compose contains the expected digest-pinned worker image.
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
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553
```

3. Replace the image reference in `dstack-compose.template.yml` with the digest-pinned worker image.
4. Publish the runtime image through the manual GitHub Actions workflow `Publish Smart Contracts Image`.
5. Copy the digest-pinned runtime image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:ecca24e8dbafbf978acdad4941b25611994d734c2087cc603399e4a44e42540f
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

During deployment, `starter_docker.sh` checks that the worker image is pinned by immutable `sha256` digest. Production registration is fail-closed until every approved worker's exact canonical `app_compose` hash is mapped to that image digest through `PHALA_ALLOWED_WORKER_COMPOSE_HASHES`.

For Phala/dstack, the measured `compose-hash` is not just the SHA-256 of `dstack-compose.template.yml`. In practice there are two related hashes:

- the RTMR3 `compose-hash` event: SHA-256 of the normalized app-code object exported from Phala into `phala/app_code.txt`
- the raw compose-file hash: SHA-256 of the `docker_compose_file` text, which can match your local `dstack-compose.template.yml`

The raw compose-file hash is not sufficient for this policy. Copy the canonical hashes reported by `dstack info` for the deployed workers into the comma-separated `PHALA_ALLOWED_WORKER_COMPOSE_HASHES` value. The bootstrap installs both the verifier's exact compose-event allowlist and the Registry mapping `composeHash -> workerImageDigest`.

For a first deployment where the hashes are not known yet, leave the value empty. Contracts and runtime endpoints are created, but worker registration remains disabled. Read each worker's canonical `app_compose` hash from its public TCB info or generated local artifact, review the measured compose, set the allowlist, and apply the runtime configuration again. A correct image-digest claim without this allowlist is deliberately rejected.

When the worker image digest changes, refresh the local Phala measurement exports before rebuilding the smart-contracts image. A live worker publishes its current RTMR3 event log and app-code export into the runtime Kubo MFS under `/phala-artifacts/latest`. Pull those into the repository with:

```bash
scripts/fetch_phala_worker_artifacts.sh "$KUBO_API"
```

This refreshes `phala/rtmr3_event_log.txt` and `phala/app_code.txt` when Phala exposes them. These files are generated, permission-restricted artifacts and must not be committed. Exports from deployments created with older templates can still contain plaintext keys; rotate those keys and redeploy before treating a new export as safe. After reviewing the canonical app-code object, provision its hash through `PHALA_ALLOWED_WORKER_COMPOSE_HASHES`; use `PHALA_EXPECTED_COMPOSE_HASH` only for a deliberate single-worker policy.

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

The on-chain contract verifies the TDX quote and replays the submitted Phala/dstack RTMR3 event digests until they reproduce the signed RTMR3 value inside the quote.

During registration, the worker submits the live RTMR3 event digests, the live `compose-hash` payload from the Phala quote response, and the worker image digest extracted from the measured `app_compose`. The attestation contract recomputes the Phala `compose-hash` event digest from that payload, checks that this event is present in the replayed RTMR3 chain, verifies that the replayed RTMR3 equals the signed RTMR3 inside the quote, and the registry checks that the submitted worker image digest matches the digest stored during `smart-contracts` bootstrap.

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

5. After changing the worker digest or measured worker compose, update both `worker_image` and `PHALA_ALLOWED_WORKER_COMPOSE_HASHES`, then redeploy the contract-runtime compose so bootstrap installs the new `composeHash -> imageDigest` policies.

Important:

- The on-chain attestation policy verifies the live worker quote and event replay, not the contract-runtime TEE quote.
- `PHALA_ENFORCE_REFERENCE_RTMR3` and raw `PHALA_RTMR3_EVENT_DIGESTS` remain legacy debugging inputs. The production authorization input is the reviewed `PHALA_ALLOWED_WORKER_COMPOSE_HASHES` list.
- If you change the worker digest or any Phala policy artifact consumed by `smart-contracts`, rebuild and republish the `smart-contracts` image too, then redeploy the contract-runtime TEE with the new runtime digest.

Current Terraform defaults in this scaffold match that target layout:

- `contracts_app_name = "master-thesis-contract-runtime-phala"`
- `worker_app_name = "master-thesis-dfl-worker-phala"`
- `worker_replicas = 3`
- `contracts_size = "tdx.small"`
- `worker_size = "tdx.small"`
