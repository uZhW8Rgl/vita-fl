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

If you already keep the deployment values in the repository-root `.env`, you can use the helper wrapper instead of duplicating secrets into `terraform.tfvars`:

```bash
bash phala/tf-env.sh init
bash phala/tf-env.sh plan -input=false
bash phala/tf-env.sh apply
```

The wrapper reads these values from `.env`:

- `PHALA_CLOUD_API_KEY`
- `W0_ACCOUNT_ADDRESS`
- `W0_PRIVATE_KEY`

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

1. Publish the DFL worker image through the manual GitHub Actions workflow `Publish DFL Worker Image`.
2. Copy the digest-pinned worker image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:<digest>
```

3. Replace the image reference in `dstack-compose.template.yml` with the digest-pinned worker image.
4. Publish the runtime image through the manual GitHub Actions workflow `Publish Smart Contracts Image`.
5. Copy the digest-pinned runtime image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:<digest>
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
5. Pin that runtime image by digest in Terraform via `smart_contracts_image = "ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:..."`.
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
