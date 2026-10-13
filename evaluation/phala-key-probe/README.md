# Dedicated Phala key observation workload

This fixture wraps the pinned production worker's `loadParticipantKey` and
`evaluation_key_probe` modules. It creates only a test RSA key. It never returns
private key bytes, KMS material, or the sealed document.

Build three immutable variants with `publish-evaluation-probes.yml`, or build
locally using the Dockerfile's `VARIANT` argument. A/B/C differ in a variant file
and therefore in image digest. Freeze the registry image/role policy before the
trial. A permitted cloud-app upgrade and DFL image admission are separate gates.
The unapproved image deliberately runs the same controlled key loader to test
whether DFL rejection also prevents recovery of its prior sealed state.

The authenticated observation/lifecycle server requires `DOCKER=phala`,
`EVALUATION_GATES_ENABLED=true`, `ACCOUNT_ADDRESS`, and a fresh test administration
token supplied in the encrypted `PRIVATE_KEY` environment field. This field is
used only as the fixture's HTTP token; no worker account secret is supplied.
Mount `/var/run/dstack.sock` and a persistent `participant-key-state` volume at
`/var/lib/vita-fl`, expose HTTPS port 8001, and supply no SSH access.

`/health` exposes process readiness only. All POST operations require the bearer
token. Key observations include fresh-challenge TDX evidence, the public RSA and
sealed-state fingerprints, a random process-start identifier, and kernel boot
identifier. The backup/reset/restore operations retain the ciphertext backup
inside this dedicated test TEE. `process-restart` exits only this fixture process;
Docker's `restart: always` restarts it.

Use the external driver in `vita-fl-td`: `hardware_probe` appraises the actual
quote, replays RTMR3, applies independently pinned workload/OS/application policy,
and queries production registry policy. `phala_lifecycle` performs scoped real
cloud upgrades and stop/start, then confirms restoration. Read that driver's
`docs/evaluation-tests.md` for campaign setup, failure oracles, and evidence scope.

The default `WORKER_BASE` is deliberately the published pre-change revision used
for the recorded October campaign. For the subsequent volatile-key evaluation,
build and explicitly pin a newly published worker base. Use the driver's
`ephemeral-key-lifecycle` profile: no participant-key-state volume, fresh apps,
and `PARTICIPANT_KEY_PROVIDER=ephemeral`. Claims version 4 identifies the
volatile fixture; its private RSA key remains in process memory. State
backup/reset/restore operations are not applicable to that profile.
