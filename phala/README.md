# Phala/dstack Worker Image Attestation

This directory contains the deployment template for generating a Phala/dstack TDX quote for the DFL worker image.

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
