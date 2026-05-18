# Phala/dstack Worker Image Attestation

This directory contains the deployment template for generating a Phala/dstack TDX quote for the DFL worker image.

## Flow

1. Publish the DFL worker image through the manual GitHub Actions workflow `Publish DFL Worker Image`.
2. Copy the digest-pinned image reference from the workflow summary:

```text
ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:<digest>
```

3. Replace `REPLACE_WITH_PUBLISHED_DIGEST` in `dstack-compose.template.yml`.
4. Deploy the digest-pinned compose file on Phala/dstack.
5. Fetch the TDX quote emitted by the worker.
6. Store the quote as `data/phala_tdx_quote`.
7. Extract its RTMR3:

```bash
scripts/extract_tdx_rtmr3.py data/phala_tdx_quote
```

8. Set the extracted value in `.env`:

```bash
EXPECTED_TDX_RTMR3=0x<96 hex chars>
```

With `EXPECTED_TDX_RTMR3` set, the local smart-contract verifier accepts only quotes whose RTMR3 matches the expected Phala/dstack application measurement.

## What This Verifies

The on-chain contract verifies the TDX quote and checks that the signed RTMR3 value equals `EXPECTED_TDX_RTMR3`.

The Docker image digest is verified indirectly through the Phala/dstack measurement chain: the compose file must pin the image by digest, Phala/dstack measures the application configuration into RTMR3, and the contract checks that RTMR3.
