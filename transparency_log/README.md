# TEE inference transparency log

This directory runs Microsoft `scitt-ccf-ledger` 0.18.1 as an independent,
single-node CCF service in virtual mode. The upstream source is pinned to commit
`b6ebc0518b59795147538bcc0cc7f67079a82d63`; it is downloaded and verified
during the Docker build instead of being copied into this repository.

The service has no worker account key and no access to `dstack.sock`. Its only
job is to register signed statements and return SCITT transparent statements
containing verifiable CCF receipts.

## Start

From the repository root:

```bash
docker compose -f transparency_log/compose.yml up --build -d
docker compose -f transparency_log/compose.yml logs -f transparency-log
```

The raw SCITT Registration API is then available at
`https://localhost:8000`. CCF creates a service certificate that clients must
explicitly trust; an unverified TLS connection is no longer accepted. For a
local diagnostic request, copy only the public certificate and verify the peer:

```bash
docker compose -f transparency_log/compose.yml cp transparency-log:/run/scitt-trust/service-cert.pem ./scitt-service-cert.pem
curl --fail --silent --cacert ./scitt-service-cert.pem https://localhost:8000/node/network | jq
```

The first start creates the CCF member identity, recovery key, service trust
store, ledger, and snapshots in the `scitt-data` volume. Later starts use CCF
recovery rather than creating a new log. Do not delete this volume if receipts
must remain retrievable.

## TLS authentication

Agent, receiver publication, receipt readback, and public trust-anchor export
use certificate-chain, validity, and hostname verification. `SCITT_DEVELOPMENT`
defaults to `0`; attempting to enable unverified TLS fails rather than falling
back. Receipt metadata cannot select the TLS policy or an unconfigured log.

The local agent reads `SCITT_CA_CERT=/run/scitt-trust/service-cert.pem` from a
read-only public-certificate volume. It never mounts the log's private data.
CCF bootstrap and health checks also verify TLS using certificates emitted by
the local CCF process, including during the transition to the open service.

For separate Phala receivers, the measured `SCITT_URL` fixes the runtime's
`https://APP_ID-8000s.GATEWAY` origin. The public hostname is included in the
CCF node certificate. `SCITT_GATEWAY_CA_BOOTSTRAP=auto` recognises that managed
Phala hostname and retrieves its public service
certificate from `https://APP_ID-8002.GATEWAY/service-cert.pem` using ordinary,
verified Web-PKI HTTPS. Port 8002 serves only that public certificate, and the
gateway authenticates its distribution. The subsequent SCITT connection uses
TLS passthrough on port 8000 and trusts only the supplied CCF certificate.
Bootstrap rejects redirects, oversized certificates, and failed TLS checks.
This authenticates distribution through the configured platform gateway; it
does not remotely attest the log process or remove trust in the gateway.

`SCITT_CA_URL` may instead name an explicitly configured HTTPS origin serving
`/service-cert.pem`. For an independently operated log with a publicly trusted
certificate, leave both bootstrap settings and `SCITT_CA_CERT` unset. For a
private CA outside the managed Phala deployment, supply `SCITT_CA_CERT` securely.
The automatic mode uses the ordinary public CA store for other log hostnames;
setting the bootstrap mode to `1` requires a managed Phala log origin.
Client certificates and roots must cover the chosen hostname and be current.
Deployments must renew and redistribute expired identities; checks never fall
back to insecure mode. A fresh disposable runtime automatically creates and
distributes a fresh certificate; this does not require persistent test state.

After changing this code, rebuild the log, agent, receiver and dynamic-worker
control images before deploying; an existing digest-pinned image does not
acquire these changes.

## Prototype security boundary

Virtual mode provides SCITT/CCF ledger semantics and cryptographic receipts,
but it does not attest the host executing the log. A single operator may also
attempt a split view unless checkpoints are observed by an independent monitor
or witness. This deployment is appropriate for the end-to-end thesis prototype;
the stronger deployment is a separately operated multi-node CCF service or a
witnessed/cross-logged checkpoint.

The log proves that a signed statement was registered and ordered. It does not
by itself validate Intel DCAP collateral. The AIR/TDX verifier must decide what
is accepted before submission, or DCAP verification must be added to an
admission gateway.

## Inference statement profiles

The MCP integration submits the deterministic CBOR evidence bundle as the
payload of an X.509-signed SCITT statement with content type
`application/vnd.master-thesis.tee-inference-evidence+cbor`. The transparent
statement returned by SCITT is stored alongside the evidence bundle and
verified against the service keys from `/.well-known/scitt-keys` before the MCP
call returns. The outer X.509 identity identifies the submitting agent; the AIR
receipt embedded in the payload remains the TEE execution identity.

After local EZKL verification, the agent also submits a canonical ZK proof
bundle with content type
`application/vnd.master-thesis.zk-inference-proof+cbor`. It contains the proof,
public input, settings, verification key, model identity, and hashes of the
proof artifacts. It does not contain the private witness. In both profiles the
MCP tool returns success only after verifying the CCF receipt for the submitted
statement.

The runtime agent maintains a fresh, non-authoritative JSONL read model of
verified submissions for the UI's **Transparency Log** iframe. The prototype
intentionally starts this display index and the SCITT service with fresh state
for each runtime container deployment.

Upstream project: <https://github.com/microsoft/scitt-ccf-ledger>
