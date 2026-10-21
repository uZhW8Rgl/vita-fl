# Independent attested key service (Anvil development profile)

The optional `PARTICIPANT_KEY_PROVIDER=broker` profile restores the participant's
random RSA, action, Sello and AIR keys across approved worker starts. Worker
ciphertext survives on its volume; service roots remain in RAM for the current
development/test setup. The service and Anvil intentionally start fresh after a
restart. This reset is a test configuration, not a requirement for production
key management. Persistent service-root custody is not implemented here.

## Current protocol: onchain-v3

The worker first authenticates the service, then submits its own attested
admission. No preselected participants or administrator-issued per-worker grants
are required. Anyone may register by satisfying the common workload policy and
participant-authorisation checks. The first successful admission records the
participant/app/manifest and public identity; restoration cannot replace them.
Registry registration remains the decision granting DFL membership.

1. The service generates an ephemeral Ed25519 signing key and instance ID.
   Before creating any worker-root store or listening, it quotes the signer,
   policy hash, instance and contract context and positively appraises its own
   evidence against independently supplied runtime references. Missing/malformed
   policy, failed quote authentication, a debug guest, changed runtime or an
   unavailable verifier stops startup. There is no production verification bypass.
2. The worker sends a fresh random challenge to `/v3/attestation`. The service's
   fresh quote binds that challenge, the signing key and instance, the exact
   runtime Compose/app ID, trust-policy hash, chain, registry, admission contract
   and its code hash. Evidence expires after at most 120 seconds.
3. The worker verifies the proof signature, expected context, platform references,
   RTMR3 replay, measured manifest and pinned service image using the existing
   fail-closed Phala HTTPS quote appraiser. The complete co-located runtime must
   use digest-pinned images. The worker checks the contract code against its RPC.
4. All `/v3/challenge` and `/v3/release` responses, including refusals, are signed
   by the attested key over the exact payload, HTTP status, operation, request
   nonce and service instance. Substitution, changed status, cross-request replay
   and responses from another service instance fail authentication. HTTPS remains
   enabled; message signatures bind the application endpoint beyond a gateway.
5. The worker creates its four random keys only on first initialisation. Its
   public identity, participant credential, fresh release recipient and current
   service signer/instance are bound to one worker quote. Restoration uses the
   recorded public identity and does not need an unavailable old action secret.
6. `AttestedKeyAdmission.admit` verifies the participant signature, common image
   and role policy, actual app ID, exact manifest, quote and release context.
   The worker's DCAP appraisal runs once on Anvil. The service does not estimate
   gas or pre-execute quote verification. Registry registration reuses this
   admission without a second worker-quote appraisal.
7. Before root creation/use the service requires a valid self-appraisal again.
   A positive self-appraisal is cached for at most 60 seconds; expiry requires a
   fresh positive appraisal. The first worker admission creates an independent
   random root; restoration requires the existing in-memory root. HKDF binds its
   wrapping key to chain, policy, service, registry, participant, app and Compose.
8. The wrapping key is encrypted to the quoted X25519 worker recipient. The worker
   verifies the signed response, decrypts or creates its AES-GCM bundle, compares
   all four public identities with admission, and creates private runtime files
   only in `/run/vita-fl` tmpfs. The root itself is never sent to the worker.

A lost service root or worker bundle fails restoration instead of creating a
replacement identity. Initial admission followed by a crash before the first
bundle write still needs an explicitly fresh identity; no automatic repair is
claimed. This prototype does not erase plaintext previously disclosed by approved
code. Its service self-check gates current RAM-root use; it does not implement an
external protection boundary for future persistent root restoration.

Worker admission's on-chain DCAP check and the service's self/client quote
appraisals have different subjects. RA-TLS and AIR checks elsewhere keep their
own purposes. Adding service authentication does not add another worker DCAP
verification during registration.

## Independent service references and deployment

`render_runtime.py` adds the unprivileged, read-only service to the existing
contract-runtime, with read-only access to `/var/run/dstack.sock`. It adds no root
or Anvil-state volume. The ordinary Foundry starter deploys the admission contract
when `KEY_SERVICE_ENABLED=1`; the service receives no administrator wallet or
Cloud API credential. All co-located images must be reviewed and digest pinned.

Supply public service trust references as `KEY_SERVICE_TRUST_POLICY`. Its JSON
contains `version`, runtime `appId`, exact `composeHash`, `serviceImage`, HTTPS
`origin`, and independently accepted `platformMeasurements` (MRTD/RTMR0--2).
It lists service software, not permitted workers. An initial reference is never
accepted merely because the contacted service supplied it.

Use the exact canonical manifest and Compose hash independently returned during
provisioning, before starting the service. Example:

```sh
python -m key_service.render_service_policy canonical-runtime.json service-policy.json \
  --app-id 0xRUNTIME_APP_ID --origin https://keys.example \
  --platform-policy runtime-platform-policy.json --expected-compose-hash COMPOSE_HASH
```

Supply the resulting policy separately through the encrypted runtime environment;
rendered Compose contains only `${KEY_SERVICE_TRUST_POLICY}`. Embedding its own
expected Compose hash literally into that manifest would create a hash cycle.
The policy hash is itself bound in every service quote. Workers receive the same
independently approved public policy in their measured configuration:

```sh
python -m key_service.render_worker base-worker.json broker-worker.json \
  --origin https://keys.example --policy 0xADMISSION_CONTRACT \
  --service-id SERVICE_ID --service-trust-policy service-policy.json
```

The worker policy binds this value like the other fixed security settings;
participant-specific identities remain variable. Service and worker use distinct
platform references because their OS images can differ. Do not copy the worker
OS policy as the runtime reference without checking it.

The service's own measurements cover its complete CVM configuration. Co-located
Anvil and other services remain in the trusted runtime; the implementation does
not add an independent ledger, hardware isolation between containers or a KMS
self-governance contract. Restricted RPC and policy administration remain trusted.
The existing platform still provisions CVMs; this service neither replaces
Phala's KMS nor needs a mainnet bridge.

## Verification and version boundaries

```sh
python -m pytest -q key_service
node --test key_service/*.test.mjs
forge test --root smart_contracts --match-contract AttestedKeyAdmissionTest
node --test key_service/admission.integration.mjs
```

The local integration uses actual Anvil transactions, HTTP, signatures and sealed
bundles, with explicitly labelled synthetic quote fixtures. It checks enrolment
without a prior grant, single worker appraisal, service-response authentication,
restoration and denial after service-root loss. These fixtures do not establish
successful hardware attestation of the new service-authentication path.

The **onchain-v3** Phala campaign passed **45/45 assertions** through
SMEW → SMA → vita-fl-td (probe image source `959cbeea`, driver `41e943a`). It checks
initial four-key admission, process and CVM restart, refusal after image replacement
and a forged Compose claim, and restoration of the original four keys after the
original image returns. Each of the six cases includes independent service-quote
appraisal. Six additional checks against a fresh genuine service proof reject
nonce replay, altered payload/status/operation, a different expected manifest,
and an attacker-signed attempt to rebind the genuine quote to another signer.

Evidence is retained locally in
`vita-fl-td/validation/chapter7-attested-service-20261021/`; the successful measured
run is `smew-run-02`. The first worker run is retained as an inconclusive error:
its image lacked PyYAML and completed no assertions. Both the probe and regular
worker Dockerfiles now install the appraiser dependencies and check its import
during the build. The deployment driver also preserves the measured `allowed_envs`
ordering when updating Phala's encrypted environment. The cleanup record confirms
test-worker deletion and runtime shutdown.

The prior **onchain-v2** campaign passed 39 assertions at `cfc8d563`; the older
**online-v1** campaign passed 38. Their retained evidence describes those historical
protocols. New deployments need the v3 images, updated contracts and independent
service references. No silent downgrade or automatic migration of old participant
bundles is supported. Archived source/images preserve the historical runs.

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

## Historical online-v1 worker configuration and grant

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

## Historical online-v1 verification and cloud experiment

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

The current **memory-only onchain-v2** deployment uses revision `cfc8d563` and
[image publication 39615569211](https://github.com/uZhW8Rgl/vita-fl/actions/runs/39615569211).
The full CI passed at the same revision. The real Phala campaign passed **39/39
assertions** through SMEW → SMA → vita-fl-td. Its local Anvil integration explicitly
checks that a service restart denies restoration instead of creating replacement
roots. The dedicated cloud evidence and final cleanup record belong in the
sibling driver's `validation/chapter7-onchain-key-service-20261021-final/`
directory. The runtime is stopped after the experiment and the dedicated worker
is deleted; restarting it starts with empty RAM roots and ephemeral Anvil state.

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
