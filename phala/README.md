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

Minimal `terraform.tfvars`:

```hcl
account_address    = "0xYOUR_WORKER_ADDRESS"
private_key        = "0xYOUR_WORKER_PRIVATE_KEY"
registry_address   = "0xYOUR_REGISTRY_ADDRESS"
aggregator_address = "0xYOUR_AGGREGATOR_ADDRESS"
gm_storage_address = "0xYOUR_GM_STORAGE_ADDRESS"
sepolia_rpc_url    = "https://sepolia.infura.io/v3/YOUR_KEY"
```

Notes:

- Fill in the contract addresses, worker key, and Sepolia RPC URL before applying.
- `worker_image` should stay pinned to a `sha256` digest to preserve a stable measured compose policy.
- The Terraform scaffold now separates `smart-contracts` and `dfl-worker` into different Phala apps / TEEs.
- If you want SSH access, set `ssh_public_key_path`; if you also want the key stored account-wide in Phala Cloud, set `manage_account_ssh_key = true`.
- The current scaffold injects worker configuration through the rendered compose file so it stays close to your existing manual deployment flow.
- The default minimal hardware profile is now `tdx.small` with `20 GB` disk.

### Where Hardware Is Selected

The Phala hardware and placement are chosen through the following Terraform inputs:

- `size`: the machine class, for example `tdx.small`, `tdx.medium`, or a GPU family size
- `region`: deployment region such as `US-WEST-1`
- `os_image`: the Phala base image slug
- `disk_size`: attached storage size in GB
- `replicas`: how many identical CVMs should run
- `node_id` (optional): pinning to one specific worker node

In this scaffold those values are wired into `resource "phala_app" "contract_runtime"` and `resource "phala_app" "dfl_worker"` in `main.tf`.

## Flow

1. Publish the DFL worker image through the manual GitHub Actions workflow `Publish DFL Worker Image`.
2. Copy the digest-pinned image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:<digest>
```

3. Replace the image reference in `dstack-compose.template.yml` with the digest-pinned image.
4. Deploy the digest-pinned compose file on Phala/dstack.
5. Fetch the TDX quote emitted by the worker.
6. Store the quote as `data/phala_tdx_quote`.
7. Optionally inspect its RTMR3:

```bash
scripts/extract_tdx_rtmr3.py data/phala_tdx_quote
```

8. Start the local stack.

```bash
docker compose down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose up --build --force-recreate
```

During deployment, `starter_docker.sh` sends `data/phala_tdx_quote` to `AutomataDcapTdxV4Attestation`. The contract parses the reference quote on-chain, extracts its signed RTMR3 value, and stores it as the expected workload measurement. The script also checks that `dstack-compose.template.yml` pins the worker image by immutable `sha256` digest and records the hash of this compose policy on-chain as `expectedComposeHash`.

The compose file is intentionally the replaceable policy input. When the worker image changes, update `dstack-compose.template.yml`, deploy that exact file on Phala/dstack, fetch the new quote, and rerun the local deployment. The local deployment will derive the new `expectedComposeHash` from the current file.

## What This Verifies

The on-chain contract verifies the TDX quote and checks that the signed RTMR3 value of registering workers equals the RTMR3 extracted from the reference quote.

The compose hash is represented on-chain as policy metadata through `expectedComposeHash`. A complete image-to-RTMR3 verification additionally requires the Phala/dstack RTMR3 event log: the verifier must replay the RTMR3 measurement chain and confirm that the measured compose hash matches the expected compose policy hash.

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
5. Deploy the contract-runtime TEE with `anvil`, `ipfs`, and `smart-contracts`.
6. Let `smart-contracts` load the worker-reference policy artifacts on-chain:
   - expected RTMR3 from the worker reference quote
   - expected compose hash / compose event digest from the worker-reference app-code and RTMR3 event log
7. Deploy the three worker TEEs, all using the same worker image and same measured worker compose policy.

Important:

- The on-chain attestation policy must point to the worker TEE policy, not the contract-runtime TEE policy.
- If you change the worker image digest or the measured worker compose file, you must refresh the worker-reference quote, app-code, and RTMR3 event log before relying on policy verification again.
