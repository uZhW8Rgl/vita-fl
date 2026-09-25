# Attested inference transport

`TEE_TRANSPORT_MODE=ratls` selects the VITA-FL attestation-bound TLS profile for
TEE inference. Evidence is exchanged on the established TLS connection before
protected HTTP traffic. It is not embedded in an X.509 certificate extension.
The legacy `mtls` mode and the ZK receiver retain certificate authentication.

## Connection and authorization

1. The receiver creates an ephemeral P-256 TLS key inside its measured CVM.
   nginx terminates TLS 1.3 in that CVM and forwards only to the private Unix
   socket. Phala ingress uses TLS passthrough (`-8443s`).
2. The agent obtains the expected origin and Sello key from its admission-bound
   receiver registry. It connects without relying on the self-signed certificate
   as a Web-PKI assertion. Only a random 32-byte challenge is sent at this stage.
3. `POST /v1/attestation` returns canonical CBOR containing session claims,
   a fresh TDX Quote V4, event log and application Compose. Claims bind the
   challenge, actual TLS SPKI hash, AIR key, Sello key, origin and bounded lifetime.
   Quote `REPORTDATA` is SHA-512 of the versioned domain and canonical claims.
   dstack may append zero padding after the declared V4 signature section. The
   verifier accepts only zero padding, matching the on-chain V4 parser, and
   retains the original bytes in evidence and quote hashes.
4. The agent verifies the actual peer SPKI, its challenge, the admitted Sello
   key, origin and lifetime. Phala's official HTTPS API checks quote cryptography;
   the agent retrieves the raw quote identified by the returned checksum from
   the same allowed HTTPS service and requires an exact byte match, then checks
   the parsed quote fields. The checksum is treated as a service identifier,
   not assumed to equal the local SHA-256 of the uploaded bytes. It also
   rejects debug mode and enforces explicit MRTD/RTMR0–2, RTMR3, Compose, image
   and contract/RPC policy. No protected headers or body have been sent yet.
5. On that same connection the agent sends the Sello token and a signed
   `X-Vita-PoP` proof. Implicit reconnects and redirects are rejected. The
   bootstrap has a 30-second maximum deadline, additionally bounded by the
   caller's timeout.
6. The receiver checks a live session, the proxy-supplied TLS SPKI, the
   independently provisioned agent registry and the PoP/token bindings before
   model loading, job access or inference.

The PoP format is **VITA-FL PoP v1**, not RFC 9449 DPoP. Its Ed25519 signature
binds subject, exact HTTP method, URL including query, body hash, token hash,
session ID, issuance time and unique proof ID. `cnf.jkt` identifies the registered
agent public key. `cnf.x5t#S256` is accepted only on the separate legacy mTLS path.
Possession of the token issuer's signing seed does not authorize a different
registered agent identity.

## AIR and audit evidence

AIR bundle v2 retains fields 2–10 from v1, sets field 1 to `2`, and adds:

| Field | Value |
|---|---|
| 11 | Exact session evidence bytes |
| 12 | SHA-256 of those bytes, as 32 bytes |

Its request nonce is the agent's session challenge. Its quote `REPORTDATA` is
`SHA256(domain_v2 || AIR_key || manifest_hash || session_id || TLS_SPKI_hash)`
followed by the 32-byte request hash. The AIR signature binds the result and
this quote's hash. The agent verifies the inference quote through Phala as well
and reconciles the receipt's signed measurements with the quote.

Sello receipts include the session ID and TLS SPKI hash in their encrypted,
signed service fields. The receiver publishes each receipt before returning
success. After verifying it, the agent additionally registers and exports the
corresponding session evidence for audit. The AIR v2 bundle includes its own
session evidence when registered in the transparency log.

The stored verifier assessment is an **agent observation of an HTTPS API
response**, not a Phala-signed statement. Quote cryptography is delegated to
Phala; a later auditor can reverify the retained raw quotes independently.

## Operation and validation

See [Phala provisioning](../phala/README.md#mandatory-inference-authentication)
for public measurement policy, registered agent keys and secret provisioning.
Missing or empty measurement policy fails closed. Keys rotate by restarting the
receiver and its session cache together before certificate expiry. Sessions
expire after at most 300 seconds. Full session/replay caches reject new entries;
live sessions are not evicted. Use the supplied single-worker server: replay
state and sessions are process-local and must be reset together.

The automated suite uses real TLS sockets, nginx configuration checks, real
Ed25519/HPKE operations and hostile evidence fixtures. Intel hardware and the
Phala HTTP verifier are substituted in those tests. A deployment still needs
approved platform measurements and a live TDX/Phala acceptance test.
