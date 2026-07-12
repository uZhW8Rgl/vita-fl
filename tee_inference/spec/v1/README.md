# ChestMNIST AIR workload profile v1

## Hash boundaries

All three protocol objects use deterministic CBOR as specified by RFC 8949
Section 4.2. A decoder must reject non-deterministic encodings, duplicate map
keys, unknown map keys, wrong field lengths, and trailing bytes.

- `model_hash = SHA-256(model_manifest_cbor)` with AIR model-hash scheme
  `sha256-manifest`.
- `request_hash = SHA-256(inference_request_cbor)`.
- `response_hash = SHA-256(inference_response_cbor)`.

These 32-byte values are copied without text or hexadecimal conversion into
AIR claims `-65539`, `-65540`, and `-65541`, respectively. The AIR receipt is
therefore cryptographically bound to both the selected model and the exact
workload input/output bytes.

The SHA-256 digest of the exact attestation document is AIR claim `-65542`.
The AIR signing public key is not self-authenticating: bundle verification must
bind it to TDX `REPORTDATA` (directly or through a quote-bound key manifest) and
must verify the quote before accepting the AIR signature as TEE provenance.

## Input and preprocessing

The request carries exactly 784 unsigned grayscale samples in row-major order.
For each pixel `p`, the service computes `(float64(p) - 127.5) / 127.5`, then
casts the resulting `[1, 784]` tensor to float32 before ONNX Runtime execution.
This matches `dfl/neural_network/cli.py`. Keeping source pixels in the request
avoids cross-language CBOR floating-point ambiguities.

## Output

The ONNX model returns 14 logits. Logits and sigmoid probabilities are encoded
as 14 consecutive IEEE-754 binary32 values in little-endian order. Decisions
are 14 bytes (`0x00` or `0x01`) and use `sigmoid(logit) >= 0.5` independently
for every label. Their fixed order is:

0. atelectasis
1. cardiomegaly
2. effusion
3. infiltration
4. mass
5. nodule
6. pneumonia
7. pneumothorax
8. consolidation
9. edema
10. emphysema
11. fibrosis
12. pleural
13. hernia

## Replay and freshness

The 16-byte request identifier correlates the response. The 32-byte
cryptographically random client nonce prevents a previously recorded request
from satisfying a fresh challenge. The timestamp is policy input, not a source
of uniqueness. Verifiers must independently enforce nonce uniqueness and their
accepted timestamp window; merely checking the AIR signature is insufficient.

The response copies the request identifier and manifest hash and additionally
contains the request hash, preventing output substitution between requests.
