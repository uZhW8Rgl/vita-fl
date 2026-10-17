# Persistent participant identity: investigation and deployment boundaries

This record describes the investigation through 2026-10-15. It complements
Chapter 7 of the thesis and the operational instructions in `README.md`.
It does not change the status of the completed volatile-key cloud campaigns.

## Why recover the same keys?

The volatile-key design creates new RSA, action, Sello and AIR keys at each
workload lifetime. That prevents the tested replacement workloads from using
the old RSA decryption capability, but also prevents an otherwise approved
restart from recovering it. Existing model-key wrappers still target the old
RSA public key. Frozen enrollment and run-recipient bindings cannot simply be
rewritten with the replacement signing and receipt keys.

The continuity target is therefore to keep the same random participant-key
bundle across approved restarts, without exposing it to the participant's
infrastructure operator or an unapproved workload. Encryption of a retained
bundle is useful only if its wrapping secret remains subject to that rule.

Scores are recorded for the logical participant address. Changing a workload
key does not automatically erase a score. Restoring the existing bindings can
preserve its accumulated history, which is checked in the local contract
fixture. However, an operator can deliberately create a new wallet and app.
Without a separate scarce credential, identity binding or other admission
mechanism, open image-based enrollment does not prevent abandonment of an
unfavourable identity. Stable keys alone are not Sybil resistance.

## What the historical recovery actually showed

The October 12 SMEW/SMA/vita-fl-td campaign generated a random RSA test key in
a Phala workload. It retained the encrypted key document in explicitly
preserved application storage. A changed image with the same app identity
obtained compatible material through the local dstack `GetKey` interface and
unsealed that document, although the production DFL policy parser rejected
the image. The result remains a substantive FAIL.

The random RSA private key was not reconstructed directly from the KMS.
Recovery required both its retained ciphertext and compatible wrapping
material. A fresh unrelated app did not inherit the same RSA identity.
The experiment did not recover a production model key, export a private key,
or perform complete fresh DCAP enrollment of the rejected image. Nor did it
observe whether the guest agent made a new remote KMS request or reused
already provisioned app material. These limits remain important when designing
the replacement-image control for any new KMS profile.

The key hierarchy explains the distinction: a secret-holding KMS has root
material; application-specific signing material is derived under the app ID;
the guest agent derives requested material under a caller-selected path.
In the inspected dstack v0.5.9 implementation the application signing-key
derivation is not itself changed by a new Compose hash. The same app ID is
therefore not an assertion that the same code is running. This is not a
universal secret shared with all users or applications.

## Why a normal operator-controlled allowlist is insufficient

Each participant is intended to run the workload in its own Phala account
and retain full account-operation rights. The participant must not also be
able to broaden the key-release policy for that app. If it owns a mutable
`DstackApp` allowlist and adds a malicious Compose hash, the KMS would be
following that relaxed policy when returning app secrets.

The optional branch instead fixes the app's exact approved Compose in a
non-upgradeable `WorkerAppAuth`. The app contract has no hash-addition,
ownership-transfer or upgrade operation. Its factory accepts the shared image
and role policy rather than a shortlist of participant addresses. Creating
such an app is not membership: live quote-bound registration must still bind
the participant, action key, RSA and receipt keys, app ID, Compose and trusted
KMS identity. The same-key renewal path preserves the participant's score.

The runtime creates all four application key types randomly once and retains
an AES-GCM-encrypted bundle. The authenticated context includes app ID,
Compose, participant, registry and chain. It restores plaintext only into the
private runtime tmpfs. Tampering, context changes, or corrupt retained state
fail rather than silently generating a new identity. These application checks
do not make it safe to issue the wrapping key to hostile code: the external
release boundary must work before an unapproved replacement can use it.

## Implemented and checked locally

The original dstack `DstackKms` implementation and ERC1967 proxy run on Anvil.
BootAuth converts KMS boot information into calls to that contract, which
delegates to the immutable per-app policy. The HTTP integration executes real
transactions with synthetic boot inputs. It accepts the approved configuration
with another instance ID and denies changed Compose, OS, app and TCB contexts.
It also checks KMS self-admission, two operators and shared-policy withdrawal
in the pre-enrollment fixture.

Local runtime tests restore RSA/action/receipt identities, decrypt the original
test ciphertext, and reject tampered state or changed context. The score test
uses the actual model ledger with simulated attestation. None of these tests
is a genuine DCAP key-provisioning experiment. Full regression counts and
overlapping helper invocations are retained in
`validation/local-checks-20261015.json`.

The real dstack KMS v0.5.9 image was downloaded, checked against its digest and
revision, and used for a prepared CVM manifest and a startup-syntax check.
The service was not bootstrapped. Its manifest keeps attestation, OS-image
verification and self-authorization enabled. Root-key storage selects the
local SGX provider; a host must actually offer this service. An ordinary
operator-relaxable app-key policy around the KMS roots would only move the
original problem to the service itself. The experiment host did not offer
`/dev/kvm`, `/dev/tdx_guest` or `/dev/sgx_enclave`, so it could not replace the
managed Phala host with a local hardware-backed deployment.

## Observed Phala restriction

On October 15, reading the stopped runtime CVM's network configuration returned
HTTP 200. PATCHing its **exact current KMS URLs**, with `restart=false`, returned
HTTP 403: `Updating KMS URLs is not enabled for this workspace`.
No CVM was started, created or assigned another KMS. The sanitized record is
`validation/phala-preflight-20261015.json`.

This is a workspace feature restriction, not a measured key-policy rejection.
No publicly documented self-service enablement was found. Phala support would
need to clarify enablement and compatibility. Enabling the URL field would
not by itself establish local-provider availability, app-ID provisioning,
compatible OS/attestation support or network reachability. Alternatively, a
controlled dstack VMM host could supply the deployment path.

## Why Anvil, Ethereum and Sepolia are separate questions

Deploying the KMS contracts on Anvil proves their EVM portability. Phala's
managed KMS does not automatically consult that separate chain. Its catalogue
visible to this account contained 40 KMS entries; on-chain entries used chain
IDs 1 (Ethereum Mainnet) or 8453 (Base), and none 11155111 (Ethereum Sepolia).
The complete sanitized catalogue is retained in
`validation/managed-network-catalog-20261015.json`. This is a dated service
observation, not a claim that dstack cannot use Sepolia. The upstream
self-hosted governance guide explicitly uses Sepolia.

A considered relay could carry Anvil decisions to Ethereum. It would require
an authorized message source, deployment and chain-domain binding, freshness,
replay protection and a failure policy. Ethereum would authenticate receipt
of that relay's statement, not independently establish the underlying Anvil
history. A fork of Ethereum in Anvil does not publish local state back to the
public network. No such relay or bridge was implemented.

The preferred further architecture keeps learning, scores and coordination
on Anvil and places only shared KMS workload policy and immutable app
contracts on **Ethereum Sepolia**. This split was discussed, not deployed.
The current app factory calls its registry on the same chain; replacing that
address or RPC URL does not make an Ethereum contract read Anvil. Policy and
factory bindings need an authenticated cross-chain link or an independently
verifiable reference. A Sepolia policy deployment would also still require a
KMS actually configured to read Sepolia: an own service, or a provider offering
that network. Neither is established by the current hosted catalogue.

## Result and unresolved live boundary

The local implementation establishes software feasibility of stable encrypted
key state and restrictive policy decisions. It supplies no successful cloud
restoration/denial measurement, KMS outage measurement or release-overhead
measurement. The completed cloud evaluation continues to describe the
volatile-key design. The new tests and probes are separate local/provider
checks, not additional SMEW/SMA scenarios or cloud assertion counts.

A conclusive comparison needs a genuine unchanged-workload recovery control
and a changed replacement denied the old decryption capability. It must cover
retained disks, cached guest material and reuse of an already provisioned
local key service, because a later KMS denial cannot revoke an already held
secret. Provider unavailability must remain distinguishable from authenticated
policy rejection. These are unverified deployment conditions, not successful
security outcomes.

## Primary sources

- [dstack v0.5.9 application-key derivation](https://github.com/Dstack-TEE/dstack/blob/v0.5.9/kms/src/crypto.rs)
- [Guest-agent key derivation](https://github.com/Dstack-TEE/dstack/blob/v0.5.9/guest-agent/src/rpc_service.rs)
- [Original DstackApp policy](https://github.com/Dstack-TEE/dstack/blob/v0.5.9/kms/auth-eth/contracts/DstackApp.sol)
- [Deployment and local provider](https://github.com/Dstack-TEE/dstack/blob/v0.5.9/docs/deployment.md)
- [Sepolia governance example](https://github.com/Dstack-TEE/dstack/blob/v0.5.9/docs/onchain-governance.md)
- [Phala hosted networks](https://docs.phala.com/phala-cloud/key-management/cloud-vs-onchain-kms)
- [Phala network-configuration API](https://docs.phala.com/api-reference/cvms/update-kmsgateway-urls)
