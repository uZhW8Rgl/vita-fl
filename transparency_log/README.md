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
`https://localhost:8000`. It uses a service-generated development certificate,
so local diagnostic requests need `curl --insecure`:

```bash
curl --fail --silent --insecure https://localhost:8000/node/network | jq
```

The first start creates the CCF member identity, recovery key, service trust
store, ledger, and snapshots in the `scitt-data` volume. Later starts use CCF
recovery rather than creating a new log. Do not delete this volume if receipts
must remain retrievable.

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

## AIR statement profile

The MCP integration submits the deterministic CBOR evidence bundle as the
payload of an X.509-signed SCITT statement with content type
`application/vnd.master-thesis.tee-inference-evidence+cbor`. The transparent
statement returned by SCITT is stored alongside the evidence bundle and
verified against the service keys from `/.well-known/scitt-keys` before the MCP
call returns. The outer X.509 identity identifies the submitting agent; the AIR
receipt embedded in the payload remains the TEE execution identity.

Upstream project: <https://github.com/microsoft/scitt-ccf-ledger>
