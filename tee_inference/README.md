# TEE inference protocol

This directory contains the versioned wire protocol for attested ChestMNIST
inference. Version 1 deliberately separates three byte strings:

1. the deterministic-CBOR model manifest, whose SHA-256 digest is the AIR
   `model_hash` using the `sha256-manifest` scheme;
2. the deterministic-CBOR inference request, whose SHA-256 digest is the AIR
   `request_hash`; and
3. the deterministic-CBOR inference response, whose SHA-256 digest is the AIR
   `response_hash`.

The CDDL definitions are closed maps: implementations must reject duplicate or
unknown keys. Hashes always cover the exact transmitted CBOR bytes. See
[`spec/v1/README.md`](spec/v1/README.md) for the complete profile.

## AIR receipts

`tee_inference.air.v1` emits and verifies the AIR v1 envelope as a tagged
COSE_Sign1 object with a CWT/EAT payload and Ed25519 (`alg=-8`). Verification is
fail-closed across four boundaries: deterministic CBOR/COSE structure,
signature, AIR claims, and caller policy (nonce, hashes, platform, model,
security mode, and freshness).

The verifier intentionally accepts a public key as explicit trust input. A
successful signature check alone does **not** prove TEE execution. The later
Phala bundle layer must establish that this AIR signing key belongs to the
attested workload, and must compare `attestation_doc_hash` and the TDX
measurements with the supplied quote before trusting the receipt.

Install the isolated dependencies and run all protocol/AIR tests with:

```sh
python3 -m pip install -r tee_inference/requirements.txt
PYTHONPATH=. python3 -m unittest discover -s tee_inference/tests -v
```

The AIR tests include the official cyntrisec/air-v1
`valid/v1-tdx-with-nonce.json` receipt and its published verification key.

## Native PyTorch inference service

`tee_inference.service` exposes `POST /v1/infer` with media type
`application/cbor`. It rejects JSON, oversized bodies, non-deterministic CBOR,
unknown fields, a mismatched manifest hash, and an incompatible native model
contract. `GET /healthz` reports the loaded model and manifest SHA-256 values.

At startup, the service reads both the model and its exact canonical manifest;
it checks the manifest's `aggregated.bin` SHA-256 and byte length before serving requests.
The required environment variables are paths rather than trusted digest text:

```sh
export DATASET_NAME=chestmnist
export TEE_MODEL_PATH=/app/model/aggregated.bin
export TEE_MODEL_MANIFEST_PATH=/app/model/model-manifest.cbor
PYTHONPATH=. python3 -m tee_inference.service
```

The HTTP response is the exact canonical `inference-response` object. AIR
emission will wrap its SHA-256 in the attested Phala service layer without
changing these response bytes.

### Container publication

The `Publish TEE Inference Image` GitHub Actions workflow tests the protocol,
builds `tee_inference/Dockerfile` for `linux/amd64`, and publishes:

```text
ghcr.io/uzhw8rgl/master-thesis-tee-inference:tee
ghcr.io/uzhw8rgl/master-thesis-tee-inference:<git-commit-sha>
```

It prints the digest-pinned Phala reference in the workflow summary. A manual
`workflow_dispatch` can override `tee`; pushes to the `tee_inference` branch
that change the service or its workflow publish automatically. The image runs
as UID/GID 10001 and contains no model, private key, or dstack socket. Model and
manifest must be supplied together at `/app/model` (or through the two path
environment variables above).
