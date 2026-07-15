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

`tee_inference.service` exposes a job API: `POST /v1/models/fetch` fetches and
verifies the current model, `POST /v1/jobs` selects a model-bound ChestMNIST
sample, and `POST /v1/jobs/<job-id>/run` returns the canonical AIR evidence
bundle. The older `/v1/prepare` and `/v1/infer` endpoints remain temporarily for
compatibility. `GET /healthz` reports whether a model is loaded.

Container startup does not access or load a model. `GET /healthz` remains healthy
with `model_loaded=false`. During `fetch_latest_verified_tee_model_bundle`, the
agent calls `POST /v1/models/fetch`. That request verifies W0's supplied RSA key against
W0's authorized DeviceRegistry key, reads the current GMStorage CIDs, downloads
and decrypts the encrypted IPFS bundle, verifies the aggregator signatures, and
constructs the canonical manifest from that verified state. A later tool call
refreshes the model again, so it observes the then-current on-chain model:

```sh
export DATASET_NAME=chestmnist
export ACCOUNT_ADDRESS=0x...
export RSA_PRIVATE_KEY='-----BEGIN PRIVATE KEY-----...'
export RPC_URL=https://...
export KUBO_API=https://...
export TEE_MODEL_DIR=/tmp/tee-inference/model
export TEE_JOB_DIR=/tmp/tee-inference/jobs
export CHESTMNIST_TEST_DATA=/app/data/chestmnist/test_data/test-data.npz
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
as UID/GID 10001 and contains no model or private key. W0's credentials are
supplied only through Phala's encrypted app environment; the separate
inference CVM receives its own dstack socket.
