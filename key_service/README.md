# Independent attested key service (Anvil development profile)

This optional `PARTICIPANT_KEY_PROVIDER=broker` path restores the participant's
RSA, action, Sello and AIR keys across worker executions of the **same authorised
manifest**, while the same key-service process stays alive. A different container image changes that manifest's measured hash
and cannot obtain the key that protects its predecessor's state. The existing
default ephemeral profile and the older `kms/` experiment remain separate.

This service does not call dstack `GetKey`, replace Phala's KMS URLs, submit
Ethereum mainnet transactions or require a bridge. Phala's managed provisioning
still starts the CVM; our independently administered service makes the
application's secret-release decision after boot.

## Current protocol: one on-chain appraisal and one random root per worker

The Phala runtime image starts `runtime.mjs` / `chain_server.mjs` (`onchain-v2`).
It does **not** call the Phala online quote-verification API. `AttestedKeyAdmission`
asks the existing DeviceRegistry's real DCAP verifier to appraise the quote on
Anvil, including approved OS measurements and replayed RTMR3. Anvil and its
administrator remain trusted development infrastructure; no mainnet is involved.

1. On first initialization the approved worker generates its four random private
   keys. Their public counterparts are included in a fresh challenge, together
   with participant, app ID, exact manifest hash, registry, chain, service,
   expiry and an ephemeral X25519 recipient. For restoration the public identity
   comes from the already admitted on-chain record instead.
2. The bootstrap participant credential signs that request. One fresh TDX quote
   binds the public identity and release recipient to the measured workload.
   Restoring workers do not need their unavailable action private key for this.
3. The service relays `admit` with a fixed gas limit; it does not pre-execute DCAP
   through `estimateGas` or `debugVerify`. The contract checks the immutable
   manifest grant, participant signature, nonce/expiry, app ID, full manifest,
   role policy and real DCAP evidence. It stores the public identity and permit.
4. Only after a confirmed permit does the service generate an **independent
   random 32-byte root for this participant/app**. `worker_roots.mjs` keeps it
   exclusively in process memory. Restoration uses that same in-memory root. No global secret derives worker roots.
5. HKDF derives a wrapping key from that worker root and `(chain, policy, service,
   registry, participant, appId, composeHash)`. Challenge and transport recipient
   are excluded so a new invocation recovers the same wrapping key. The root
   remains in the service; the wrapping key is X25519/HKDF/AES-GCM encrypted to
   the quoted ephemeral recipient.
6. The worker seals or restores its four-key bundle in `/var/lib/vita-fl` and
   materialises the private files only in `/run/vita-fl` tmpfs. It checks that
   all four restored public keys equal the admitted identity.
7. `registerDeviceWithKeyAdmission` consumes that recorded admission and the
   participant's delegation. **There is no second quote appraisal.** Each later
   key restoration still requires its own fresh quote and one new admission.

An unapproved replacement image is denied even when it retains the app ID and
can read the old ciphertext. Its manifest hash is also part of the wrapping-key
context. Changing an image does not automatically update a grant. Missing roots or corrupt or insecurely permissioned bundle state fail closed. In particular,
losing the sealed bundle does not create replacement keys under an admitted
identity. A crash between initial admission and the first durable bundle write
requires explicit recovery with a fresh identity; this prototype has no automatic
repair for that enrollment window. Service restarts intentionally lose all worker roots. An admitted worker then
cannot decrypt its previous bundle; the service refuses restoration instead of
creating a replacement root. Anvil is also ephemeral; a runtime restart requires
a new contract deployment, grants and worker enrollment. Revocation prevents future
releases but cannot erase material previously released to an approved worker.

## Runtime deployment and trust boundary

`render_runtime.py` overlays the existing contract-runtime manifest with an
unprivileged, read-only serving container without a state volume. The serving
process receives neither the wallet private key nor the Cloud API key.
`DeployAttestedKeyAdmission.s.sol` runs inside the existing Foundry starter when
`KEY_SERVICE_ENABLED=1`. No Node administrator deployment or additional
bootstrap wrapper is used. The overlay preserves the base Anvil and IPFS
configuration and introduces no persistence for either.

Build the images using workflow **Publish Dedicated Evaluation Probes**, branch
`kms`, input `profile=runtime-v2`. The output supplies four immutable references
(worker, replacement test variant, service and contracts). The probe is not the
default command; a dedicated probe manifest explicitly invokes
`/evaluation/phala-broker-probe/server.mjs` on the digest-pinned worker image.
Render the overlay with `python -m key_service.render_runtime BASE OUTPUT
--images IMAGES.json --platform-policy POLICY.json`. The images JSON contains
`service`, `worker`, `contracts`; the policy is the independently verified TDX OS
reference from the evaluation artifacts. Apply it only to the explicitly selected
contract-runtime CVM, retaining the complete base manifest for review/rollback.
After a new Terraform render, review and reapply this optional overlay.

Worker roots are never written to files, volumes, images, worker environments
or Anvil. They exist only in the serving process's RAM and are discarded on
restart. Hosting the service in a TEE does not by itself protect against a
malicious administrator controlling the **service's own** image or execution. This prototype therefore trusts the contract-runtime
administrator and its Docker control plane. It protects against replacement of
a worker image, not compromise of that central runtime. RPC/admin access must
stay restricted; the public RPC proxy permits signed transactions but rejects
unlocked-account and Anvil administration methods. The service uses private
Anvil RPC internally. Public client access uses HTTPS with normal certificate
validation, optionally a pinned CA.

The approved worker must keep keys private and restrict the attestation socket.
Review the whole manifest, including commands, mounts and environment
permissions. Arbitrary code injection into an approved workload remains outside
this prototype's guarantee. The default worker profile remains ephemeral until
explicitly configured with `PARTICIPANT_KEY_PROVIDER=broker`; no old identity is
automatically migrated.

## Historical online-v1 profile

The older `service.mjs`, `deploy-local.mjs` and `compose.yaml` below retain the
previous experiment: a shared service root with per-context HKDF derivation and
online Phala quote verification. The Compose command selects that legacy server
explicitly. Select `KEY_SERVICE_PROTOCOL=online-v1` only for this historical
profile. Its successful cloud experiment below does **not** validate onchain-v2.

## Local setup

Use Node 20, Python 3.12, Foundry and Docker Compose. From the repository root:

```sh
cd dfl/node_server
npm ci
npm run build
cd ../..
python3 -m pip install -r key_service/requirements.txt
forge build --root smart_contracts
docker compose -f key_service/compose.yaml up -d anvil
mkdir -m 700 key_service/private
node key_service/service.mjs init-root key_service/private/root.key
node key_service/deploy-local.mjs key_service/private/settings.json
```

`deploy-local.mjs` uses only unlocked accounts on a loopback Anvil RPC. It deploys
an isolated registry and policy, writing public pins to `settings.json`. Existing
deployments can instead use `smart_contracts/script/DeployAttestedKeyPolicy.s.sol`
with explicit admin credentials. Never expose unlocked Anvil RPC through a tunnel.

Set `KEY_SERVICE_POLICY`, `KEY_SERVICE_POLICY_CODE_HASH`, `KEY_SERVICE_REGISTRY`
and `KEY_SERVICE_ID` from those settings. Set `RATLS_ALLOWED_PLATFORM_MEASUREMENTS`
to a JSON array of independently approved `{mrtd,rtmr0,rtmr1,rtmr2}` tuples.
For the Compose service also set `KEY_SERVICE_UID` and `KEY_SERVICE_GID` to the
owner of `private/root.key`. The server rejects a missing, permissive or symlinked
root and invalid attestation configuration at startup.

```sh
docker compose -f key_service/compose.yaml up -d --build key-service
```

The service listens on `127.0.0.1:19002` and Anvil on `127.0.0.1:19545`.
Terminate HTTPS in front of the service for cloud access. A temporary
[Cloudflare Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
can be used for evaluation; expose only the service API. The application key
remains recipient encrypted even at that proxy. Stable deployments need stable
HTTPS origin, service ID, policy address, registry, root and persisted chain.
The HTTPS terminator remains trusted for service authentication; the broker
itself is not remotely attested by this prototype.

## Worker configuration and grant

Use a worker image containing the new TypeScript build. Configure
`DOCKER=phala`, `EXPECTED_CHAIN_ID=31337`, `PARTICIPANT_KEY_PROVIDER=broker`,
`SELLO_SERVICE_KEY_PROVIDER=broker`, `KEY_SERVICE_URL` (HTTPS origin),
`KEY_SERVICE_POLICY`, `KEY_SERVICE_ID`, `ACCOUNT_ADDRESS`, `REGISTRY_ADDRESS`
and encrypted `PRIVATE_KEY`. The bootstrap private key signs participant
delegation; it is distinct from the restored action key. Optional
`KEY_SERVICE_CA_FILE` pins a custom HTTPS CA without disabling verification.

Mount a persistent volume at `/var/lib/vita-fl` and a private tmpfs at
`/run/vita-fl`. `render_worker.py` can convert a reviewed single-worker app
manifest; review existing mounts before conversion. The sealed state is
`participant-keys.broker.v1.sealed.json`; no existing ephemeral state is migrated.

Before first release, authorise the **exact measured app-compose JSON bytes**
from the reviewed deployment, not a YAML file or differently serialised JSON:

```sh
node key_service/authorise.mjs exact-app-compose.json PARTICIPANT_ADDRESS APP_ID
```

The admin command requires `KEY_SERVICE_RPC_URL`, `KEY_SERVICE_POLICY`,
`KEY_SERVICE_POLICY_CODE_HASH`, and `KEY_POLICY_ADMIN_PRIVATE_KEY`. All writes
are restricted to chain 31337. Keep administrator credentials out of worker envs.

## Verification and cloud experiment

```sh
python3 -m pytest -q key_service
node --test key_service/service.test.mjs key_service/worker_roots.test.mjs
REQUIRE_ANVIL_TESTS=1 node --test key_service/admission.integration.mjs
forge test --root smart_contracts --match-path test/AttestedKeyPolicy.t.sol
REQUIRE_ANVIL_TESTS=1 node --test key_service/anvil.integration.mjs
```

Unit tests inject synthetic appraisal; the integration test uses a real Anvil
policy and HTTP protocol but **also synthetic quote appraisal**. These tests do
not establish successful hardware attestation. The dedicated
`evaluation/phala-broker-probe` image exercises the production broker initializer,
signs with all four restored keys, retains a prior RSA ciphertext, and emits
freshly quoted public observations. The separate `vita-fl-td` driver runs it
through SMA/SMEW with real Phala quotes and an actual same-app image replacement.
An HTTP outage or failed quote verification is inconclusive, never a successful
negative key-release result. Cloud results belong in the driver's validation
artifacts; they must not be conflated with revision `0a9fc985` of the thesis.

The **historical online-v1** Phala campaign on **2026-10-21** passed **38 assertions** through
SMEW → SMA → `vita-fl-td`: original initialization, process restart, CVM restart,
same-app image replacement, a replacement claiming the original Compose hash,
and return to the original image. Both replacement requests received HTTP 403
while the original sealed file remained readable and unchanged. Returning to
the original image restored the same four identities and decrypted the retained
RSA ciphertext. The full evidence is in the sibling driver's
`validation/chapter7-attested-key-service-20261021/` directory, including genuine
quotes, independent OS-reference appraisal, SMA/SMEW reports and cleanup proof.
This establishes the tested key path, not a complete training/inference run or
protection from compromise of the trusted broker host.
