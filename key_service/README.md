# Independent attested key service (Anvil development profile)

This optional `PARTICIPANT_KEY_PROVIDER=broker` path restores the participant's
RSA, action, Sello and AIR keys across executions of the **same authorised
manifest**. A different container image changes that manifest's measured hash
and cannot obtain the key that protects its predecessor's state. The existing
default ephemeral profile and the older `kms/` experiment remain separate.

This service does not call dstack `GetKey`, replace Phala's KMS URLs, submit
Ethereum mainnet transactions or require a bridge. Phala's managed provisioning
still starts the CVM; our independently administered service makes the
application's secret-release decision after boot.

## Trust boundary

The service root is an independent random 32-byte file on a **trusted service
host**, never in the replaceable worker, its encrypted environment, its image or
Anvil. Its owner must protect and back up this file. A malicious service-host
administrator can read this root: the current implementation does not claim a
TEE-protected broker or protection against that administrator. Anvil and the
policy administrator are trusted development infrastructure, not a production
consensus system. RPC/admin access must remain private.

Quote authentication uses the existing strict Phala HTTPS verifier, including
explicit allowed TDX platform measurements, debug rejection, and replay of the
quote's RTMR3. Thus the prototype depends on that verifier and the selected
dstack/TDX measured-boot interpretation. It is not a provider-neutral verifier.
Do not automatically trust platform measurements supplied by a requester.

The approved worker must itself keep the keys private and restrict the
attestation socket. Digest pinning prevents unnoticed image-tag replacement;
the administrator must review the **whole manifest**, including mounts,
commands, environment permissions and exposed interfaces. Arbitrary runtime
code injection into an approved workload is outside this prototype's guarantee.

## Release protocol

1. The worker supplies participant/app/registry/Compose identities and a fresh
   ephemeral X25519 public key. The service returns a random, one-use challenge
   with its pinned service and policy identities (60-second lifetime).
2. The worker signs the canonical claims using its bootstrap participant
   credential and obtains a TDX quote whose REPORTDATA hashes these exact claims.
3. The service checks participant ownership, chain 31337, deployed policy code
   hash, registry identity and immutable grant. It authenticates the quote,
   replays RTMR3, and compares measured app ID and Compose hash with the claims.
   The actual Compose bytes must match the measured hash and name one
   digest-pinned workload image. It rechecks policy/revocation after appraisal.
4. HKDF-SHA256 derives the application key from the independent root and
   `(chain, policy, service, registry, participant, appId, composeHash)`. Nonce
   and ephemeral recipient are excluded from this persistent derivation.
5. X25519/HKDF/AES-GCM protects release for the quoted recipient, with canonical
   claims as associated data. The root and unwrapped key are never HTTP fields.
6. The worker authenticates the stored four-key bundle and materialises private
   files in `/run/vita-fl` tmpfs. Missing permission, bad evidence or corrupted
   state aborts startup; it does not fall back to ephemeral replacement keys.

`AttestedKeyPolicy` makes a participant/app grant immutable; revocation is
permanent. There is deliberately no automatic image-upgrade migration. A later
upgrade requires a separately reviewed migration design. Deleting the sealed
bundle creates a new identity on the next authorised initialization; old
identities cannot be recovered from the service root alone. The prototype does
not prevent rollback to an older authenticated sealed bundle. Revocation denies
subsequent releases; it cannot erase keys already released to an authorised TEE.

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
node --test key_service/service.test.mjs
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

The real Phala campaign on **2026-10-21** passed **38 assertions** through
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
