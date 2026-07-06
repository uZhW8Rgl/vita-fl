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
cd phala
cp terraform.tfvars.example terraform.tfvars
export PHALA_CLOUD_API_KEY="phak_xxx"
terraform init
terraform plan
terraform apply
```

If you already keep the deployment values in repository-root env files, you can use the helper wrapper instead of duplicating secrets into `terraform.tfvars`:

```bash
bash phala/tf-env.sh init
bash phala/tf-env.sh plan -input=false
bash phala/tf-env.sh apply
```

The wrapper prefers `.env.anvil` by default, falls back to `.env` if needed, and can be pointed at a custom file with `PHALA_ENV_FILE=/path/to/file`.

The wrapper reads these values from the selected env file:

- `PHALA_CLOUD_API_KEY`
- `W0_ACCOUNT_ADDRESS`
- `W0_PRIVATE_KEY`

It also forwards the current Anvil/DFL profile settings into Terraform, including:

- `W1_ACCOUNT_ADDRESS`, `W1_PRIVATE_KEY`, `W2_ACCOUNT_ADDRESS`, `W2_PRIVATE_KEY`, ... when `WORKER_COUNT` is greater than `1`
- `INITIAL_GM_SIGNER_ADDRESS`
- `CLIENT_LIMIT`, `EPOCH`, `ROUND`
- `MODEL_SUBMISSION_DEADLINE_MS`, `GM_UPDATE_TIMEOUT_MS`, `GM_UPDATE_TIMEOUT_LOOPS`
- `AGGREGATION_UPDATE_ESTIMATE_MS`, `GM_UPDATE_POLL_MS`
- `DATASET_NAME`
- `PCCS_FMSPC`, `PCCS_FETCH`, `PCCS_TEE`, `P256_MODE`
- `DEPLOY_TDX_V4_DCAP`, `VERIFY_TDX_QUOTE_ONCHAIN`, `AGGREGATOR_TIMEOUT_REPORT_PERCENT`

This means a Phala deployment can now be driven directly from `.env.anvil` without first rebuilding a combined `.env`.

Minimal `terraform.tfvars`:

```hcl
account_address    = "0xYOUR_WORKER_ADDRESS"
private_key        = "0xYOUR_WORKER_PRIVATE_KEY"
```

Notes:

- Fill in the worker key before applying.
- `worker_image` should stay pinned to a `sha256` digest to preserve a stable measured compose policy.
- `smart_contracts_image` should also be pinned to a `sha256` digest when you want the contract-runtime TEE to be reproducible.
- The Terraform scaffold now separates `smart-contracts` and `dfl-worker` into different Phala apps / TEEs.
- If you want SSH access, set `ssh_public_key_path`; if you also want the key stored account-wide in Phala Cloud, set `manage_account_ssh_key = true`.
- The current scaffold injects worker configuration through the rendered compose file so it stays close to your existing manual deployment flow.
- The default minimal hardware profile is now `tdx.small` with `20 GB` disk.
- The contract-runtime TEE runs its own local `anvil`; the worker TEE talks to that internal runtime endpoint, not to Sepolia.
- The worker resolves `REGISTRY_ADDRESS`, `AGGREGATOR_ADDRESS`, and `GM_STORAGE_ADDRESS` from the contract-runtime TEE's Kubo manifest at `/runtime/contracts.json`.
- The worker now treats `/runtime/contracts.json` as the effective runtime-ready signal. The runtime publishes that manifest only after `smart-contracts` finished its bootstrap path, which avoids startup races even when `/runtime/ready.json` is missing on Phala.
- The worker image expects the real Phala attestation socket. In this scaffold the worker compose mounts `/var/run/tappd.sock`.
- At runtime the worker first tries `/var/run/dstack.sock` and then uses the SDK's legacy `TappdClient` over `/var/run/tappd.sock` when `dstack.sock` is not present.
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
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:b63e52610d606a29d6739b6e9339b7c4c426f4f1fee0554d92a16c0ed33af7e8
```

3. Replace the image reference in `dstack-compose.template.yml` with the digest-pinned worker image.
4. Publish the runtime image through the manual GitHub Actions workflow `Publish Smart Contracts Image`.
5. Copy the digest-pinned runtime image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:55f9f0e39f091104c7fdd76c7dd4c3c48c42cb422213652240cb2be7ffe487e0
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

During deployment, `starter_docker.sh` sends `data/phala_tdx_quote` to `AutomataDcapTdxV4Attestation`. The contract parses the reference quote on-chain, extracts its signed RTMR3 value, and stores it as the expected workload measurement. The script also checks that the worker compose policy pins the image by immutable `sha256` digest and records the measured compose policy on-chain as `expectedComposeHash`.

For Phala/dstack, the measured `compose-hash` is not just the SHA-256 of `dstack-compose.template.yml`. In practice there are two related hashes:

- the RTMR3 `compose-hash` event: SHA-256 of the normalized app-code object exported from Phala into `phala/app_code.txt`
- the raw compose-file hash: SHA-256 of the `docker_compose_file` text, which can match your local `dstack-compose.template.yml`

The local deployment therefore prefers `phala/app_code.txt` when deriving `expectedComposeHash`. If that export is missing, it falls back to the raw compose file hash. When the worker image changes, update `dstack-compose.template.yml`, deploy that exact worker app on Phala/dstack, then refresh `data/phala_tdx_quote`, `phala/app_code.txt`, and `phala/rtmr3_event_log.txt` before rerunning the local deployment.

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

The on-chain contract verifies the TDX quote and checks that the signed RTMR3 value of registering workers equals the RTMR3 extracted from the reference quote.

The compose hash is represented on-chain as policy metadata through `expectedComposeHash`. A complete image-to-RTMR3 verification additionally requires the Phala/dstack RTMR3 event log: the verifier must replay the RTMR3 measurement chain, confirm that the measured app-code object hash matches the expected compose policy hash, and then compare the replayed RTMR3 with the quote.

## Next Steps

For the planned Phala layout with one contract-runtime TEE and three worker TEEs, the next practical sequence is:

1. Deploy one worker-reference TEE first, using `dstack-compose.template.yml` with the current digest-pinned worker image.
2. From that worker-reference deployment, export the measured artifacts:
   - the TDX quote into `data/phala_tdx_quote`
   - the RTMR3 event log into `phala/rtmr3_event_log.txt`
   - the Phala app-code object into `phala/app_code.txt`
3. Verify locally that the copied artifacts are internally consistent:

```bash
python scripts/verify_phala_rtmr3.py \
  phala/rtmr3_event_log.txt \
  --quote data/phala_tdx_quote \
  --compose phala/dstack-compose.template.yml \
  --app-code phala/app_code.txt
```

4. Build and publish the `smart-contracts` image that will run in the separate contract-runtime TEE.
5. Pin that runtime image by digest in Terraform via `smart_contracts_image = "ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:55f9f0e39f091104c7fdd76c7dd4c3c48c42cb422213652240cb2be7ffe487e0"`.
6. Deploy the contract-runtime TEE with `anvil`, `ipfs`, and `smart-contracts`.
7. Let `smart-contracts` load the worker-reference policy artifacts on-chain:
   - expected RTMR3 from the worker reference quote
   - expected compose hash / compose event digest from the worker-reference app-code and RTMR3 event log
8. Deploy the three worker TEEs, all using the same worker image and same measured worker compose policy.

Important:

- The on-chain attestation policy must point to the worker TEE policy, not the contract-runtime TEE policy.
- If you change the worker image digest or the measured worker compose file, you must refresh the worker-reference quote, app-code, and RTMR3 event log before relying on policy verification again.
- If you change the worker digest or any Phala policy artifact consumed by `smart-contracts`, rebuild and republish the `smart-contracts` image too, then redeploy the contract-runtime TEE with the new runtime digest.

Current Terraform defaults in this scaffold match that target layout:

- `contracts_app_name = "master-thesis-contract-runtime-phala"`
- `worker_app_name = "master-thesis-dfl-worker-phala"`
- `worker_replicas = 3`
- `contracts_size = "tdx.small"`
- `worker_size = "tdx.small"`
