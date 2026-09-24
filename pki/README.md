# VITA-FL transport certificates

This guide describes the legacy `TEE_TRANSPORT_MODE=mtls` path. Phala TEE
inference now selects `ratls`; see [RA-TLS provisioning](../phala/README.md#mandatory-inference-authentication).
The mTLS agent and inference receiver require client certificates. Sello remains the authorization
layer: an authenticated certificate alone does not authorize a tool invocation.
The receiver binds the Sello subject and certificate thumbprint to the live peer.

## Trust and deployment boundary

- An operator controls the CA and approves each initial identity with a five-minute,
  single-use token. The token pins the exact URI SAN and, for workers, server DNS SAN.
- The endpoint generates an EC P-256 private key locally and submits a signed CSR.
  The CA never receives that private key. The root certificate is pinned by its
  SHA-256 fingerprint; there is no trust-on-first-use or disabled-verification mode.
- `operator init` keeps the encrypted root private key in an **offline directory**.
  The online CA directory contains the intermediate key and public root only.
  Neither directory belongs in a worker/agent image or this repository. Store
  them outside the checkout. Protect and back up the CA database: it records
  spent enrollment tokens and revocations.
- Agent certificates have URI `urn:vita-fl:agent:<subject>` and clientAuth.
  Worker certificates have URI `urn:vita-fl:inference:<subject>`, the exact DNS
  SAN, serverAuth and clientAuth. The latter permits certificate-authenticated
  renewal; the receiver rejects the inference role as an agent.
- Transport keys are separate from dstack action/RSA/Sello keys. Worker TLS state
  resides on the TEE's encrypted persistent volume, permissions 0700/0600; it is
  not separately sealed by this module. Agent state resides in its private
  persistent volume. Local root/process compromise remains outside mTLS protection.

## Initialize the CA on an operator machine

Dependencies: `step` CLI, `step-ca`, Python with `cryptography>=42`, and OpenSSL.
The integration test runs with step CLI 0.28.7 and step-ca 0.28.4.

Prepare a private password file without placing its value in shell history, then:

```sh
python -m pki.operator init \
  --offline /secure/offline/vita-root \
  --online /secure/online/vita-intermediate \
  --password-file /secure/operator/ca-password \
  --ca-url https://ca.example.org:9000

step-ca /secure/online/vita-intermediate/config/ca.json \
  --password-file /secure/operator/ca-password

step certificate fingerprint /secure/online/vita-intermediate/certs/root_ca.crt
```

Take the root directory offline after initialization. The online intermediate
must remain reachable at the configured HTTPS origin. It issues one-hour leaf
certificates and publishes signed CRLs at `/1.0/crl`, regenerated every 30 seconds
and immediately on revocation. The root fingerprint is public deployment policy.

## Issue narrowly scoped bootstrap credentials

Use the same subject for the agent certificate and `SELLO_OWNER_SUBJECT`; the
current agent default is `master-thesis-agent`. The worker subject is `worker-0`.
The worker DNS name must already be known: reserve/precreate the Phala app or use
a controlled TLS passthrough domain before issuing its token. Phala's endpoint
must be `https://<app-id>-8443s.<gateway-domain>`; the `s` preserves TLS to nginx.

```sh
python -m pki.operator token agent master-thesis-agent \
  --ca-url https://ca.example.org:9000 \
  --root /secure/online/vita-intermediate/certs/root_ca.crt \
  --password-file /secure/operator/ca-password \
  --output /secure/bootstrap/agent.jwt

python -m pki.operator token worker worker-0 \
  --dns APP_ID-8443s.GATEWAY_DOMAIN \
  --ca-url https://ca.example.org:9000 \
  --root /secure/online/vita-intermediate/certs/root_ca.crt \
  --password-file /secure/operator/ca-password \
  --output /secure/bootstrap/worker.jwt
```

The commands write mode-0600 files and never print bearer tokens. The optional
`--csr FILE` additionally binds the token to an already generated CSR. Deliver
each token through the recipient's encrypted deployment environment as
`PKI_ENROLLMENT_TOKEN`, or a private file named by `PKI_ENROLLMENT_TOKEN_FILE`.
Never distribute the provisioner password or signing key to an application.

## Run endpoints

Public configuration: `PKI_CA_URL`, `PKI_ROOT_FINGERPRINT`, `PKI_SUBJECT`, and
worker-only `PKI_DNS_NAME`. Persistent `PKI_STATE_DIR` defaults to
`/var/lib/vita-fl/tls` for the worker and `/var/lib/vita-fl-agent-tls` for the agent.

```sh
python -m pki.runtime run worker -- APPLICATION_COMMAND
python -m pki.runtime run agent -- APPLICATION_COMMAND
```

The wrapper performs enrollment only when persistent credentials are absent,
checks the returned certificate identity and key match, and atomically installs
the certificate/key generation. A normal restart reuses the stored identity,
without replaying the enrollment token. An interrupted first enrollment after
the CA consumed the token but before the certificate was saved needs a newly
issued token; the pending local key is retained. Expired or revoked identities
fail closed and require deliberate operator recovery, not silent reenrollment.

The wrapper supplies these paths to the child process:

| Variable | Contents |
|---|---|
| `TLS_CERT_PATH` | Current leaf plus intermediate certificate chain |
| `TLS_KEY_PATH` | Locally generated private key, mode 0600 |
| `TLS_CA_CERT_PATH` | Fingerprint-pinned root certificate |
| `TLS_CRL_PATH` | Current issuer-signed leaf CRL |
| `TLS_CLIENT_CA_CERT_PATH` | Same root, for receiver client validation |
| `TLS_CLIENT_CRL_PATH` | Same CRL, for receiver request authorization |

Certificates renew at two-thirds of their lifetime using certificate possession,
without exporting or replacing the local private key. The wrapper refreshes the
CRL every 30 seconds, validates signature, issuer, validity and rollback, then
reloads nginx. A refresh/renewal error stops the supervised processes. This
deliberately sacrifices availability when the CA cannot provide current evidence.
CRL checks currently assume agent and worker certificates share one intermediate;
intermediate rotation requires a coordinated trust-bundle migration.

## Private receiver boundary and revocation

nginx listens on HTTPS 8443, verifies the client certificate chain, and forwards
to `/run/vita-fl/inference.sock`. The application must expose **only** this Unix
socket, inside mode-0700 `/run/vita-fl`, with the socket mode 0600. Both trusted
processes run as the same container user (root in the current image). An
independent TCP listener would let callers forge proxy authentication headers.

nginx overwrites `X-Vita-Mtls-Verify` and `X-Vita-Client-Cert` with its actual TLS
verification result and URL-escaped peer certificate. `authenticated_peer()`
validates role, clientAuth usage, validity, issuer and the current signed leaf
CRL **on every request**, before the inference payload is processed. Leaf CRL
enforcement is at this private application boundary; nginx `ssl_crl` is omitted
because its chain-wide checks would also require an offline-root-issued CRL.
The agent checks server leaf revocation during the HTTPS handshake.

Revoke a compromised certificate with the operator's scoped CA authority:

```sh
python -m pki.operator revoke agent DECIMAL_SERIAL \
  --ca-url https://ca.example.org:9000 \
  --root /secure/online/vita-intermediate/certs/root_ca.crt \
  --password-file /secure/operator/ca-password
```

Use `worker` for the worker provisioner. The command creates a one-use revocation
token in memory and sends it in the HTTPS request body. The CA
blocks further renewal and publishes an updated CRL. Receivers reject newly
arriving requests after the next successful refresh (at most approximately 30
seconds plus request latency); an already authorized request is not cancelled.
To revoke an identity across renewals, revoke every still-valid serial issued
to that identity and refuse new bootstrap tokens. There is no automatic
identity-wide suspension registry in this prototype.

## Validation

```sh
python -m pytest transport_security/tests pki/tests -q
```

The TLS tests use loopback sockets and verify a real mutual handshake, HTTPS-only
requests, redirect rejection, server revocation, certificate role checks,
receiver revocation, stale/forged/rolled-back CRLs, and atomic certificate/key
installation. The nginx integration test runs the production proxy configuration
against a private Unix socket: absent/untrusted client certificates never reach
the receiver, caller-supplied identity headers are overwritten, and incorrect
roles or revoked certificates are rejected by the receiver. It skips explicitly
when nginx is absent.

The step-ca integration test starts a disposable loopback CA and checks one-use
tokens, restart without reenrollment, agent and worker certificate purposes,
renewal without key replacement, the operator's revocation command, and refusal
to renew revoked certificates. It skips explicitly when either Smallstep binary
is absent. No production CA or Phala deployment is created by these tests.

References: [step-ca configuration](https://smallstep.com/docs/step-ca/configuration/),
[one-use enrollment](https://smallstep.com/docs/step-ca/basic-certificate-authority-operations/),
[renewal](https://smallstep.com/docs/step-cli/reference/ca/renew/),
[active revocation](https://smallstep.com/docs/step-ca/certificate-authority-server-production/),
[nginx TLS variables](https://nginx.org/en/docs/http/ngx_http_ssl_module.html).
