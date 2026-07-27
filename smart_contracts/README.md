# Smart Contracts

This directory is the active Foundry project for the DFL coordination and attestation layer.

It contains the minimum Solidity and deployment bundle required by the Docker-based prototype:

- DFL coordination contracts:
  - `src/core/DeviceRegistry.sol`
  - `src/core/AggregatorSelection.sol`
  - `src/core/GMStorage.sol`
- TDX/DCAP quote verification:
  - `src/attestation/AutomataDcapTdxV4Attestation.sol`
  - `src/attestation/tdx/QuoteV4Auth/*`
- deployment and utility scripts:
  - `script/Deploy.s.sol`
  - `script/DeployTDXV4Attestation.s.sol`
  - `script/ProbeP256Verifier.s.sol`
  - `script/UploadPccsCollaterals.s.sol`
  - `script/VerifyTDXV4Quote.s.sol`
  - `starter_docker.sh`
- runtime quote/collateral input:
  - `../data/phala_tdx_quote`

The contract and attestation deployment code is consolidated here so Docker builds use one focused source tree.

## Dependency Layout

The vendored Solidity dependencies are intentionally minimized:

- `lib/forge-std`
- `lib/openzeppelin-contracts`
- `lib/p256-verifier`
- `lib/automata-dcap-v3-attestation`
- `lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs`

The PCCS subproject is also a Foundry project. Its local `lib/forge-std` and `lib/openzeppelin-contracts` entries are symlinks to the minimized top-level dependencies. This avoids duplicate vendored copies while keeping Foundry imports inside the allowed project paths.

## Build

From this directory:

```bash
forge build
```

The PCCS subproject can be checked separately:

```bash
cd smart_contracts/lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs
forge build
```

## Docker Deployment

The normal deployment path is `starter_docker.sh`, executed by the Docker Compose smart-contract service.

For the Phala flow, this image should be published through the GitHub Actions workflow
`Publish Smart Contracts Image` and then referenced from Terraform or the manual
contract-runtime compose file by immutable digest:

```text
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:e40ab86adfd33f8129d3f34b7b5643716538d38714217b2bf52b2711dea85c95
```

Rebuild and republish this image when the contracts or bootstrap code changes.
The expected worker image is supplied separately as the digest-pinned
`EXPECTED_WORKER_IMAGE` deployment input; changing that policy requires a
contract-runtime redeployment, but no measured Compose or RTMR3 digest is baked
into the smart-contract image.

It performs:

1. deployment of `DeviceRegistry`, `AggregatorSelection`, and `GMStorage`,
2. deployment/configuration of the Automata PCCS helper and DAO contracts,
3. deployment of `AutomataDcapTdxV4Attestation`,
4. authorization of the attestation contract as PCCS reader,
5. upload of PCCS collateral and configuration of the worker-image and
   role-specific worker policies,
6. publication of `/runtime/contracts.json` and
   `/runtime/admission-ready.json`,
7. waiting for live DCAP-authorized participant public keys, and
8. encryption, upload, and registration of the initial global model for those
   recipients before publishing `/runtime/ready.json`.

This split is required because worker RSA keys are generated inside their
respective TEEs and become usable bootstrap recipients only after successful
DCAP registration. `BOOTSTRAP_MIN_RECIPIENTS` selects the minimum live
recipient count (default `1`); the registration timeout, polling interval, and
optional settling window are controlled by
`BOOTSTRAP_REGISTRATION_TIMEOUT_SECONDS`,
`BOOTSTRAP_REGISTRATION_POLL_SECONDS`, and
`BOOTSTRAP_REGISTRATION_SETTLE_SECONDS`. The bootstrap derives recipients
exclusively from current `DeviceRegistry` state; it does not accept a
provisioned initial worker public key.

For the local Docker flow, `IPFS_PROVIDER` controls the bootstrap mode. With `IPFS_PROVIDER=kubo`, the deployment script signs `data/initial_gm/<dataset>/aggregated.bin`, imports model and signature into the local Kubo node, and writes those resulting CIDs into `GMStorage`. The dataset is selected through `DATASET_NAME` and defaults to `mnist`. With `IPFS_PROVIDER=pinata`, the script does not touch Kubo during initialization and instead expects `INITIAL_GM_CID` and `INITIAL_GM_SIG_CID` to already point to Pinata-hosted content.

The tested local entry point is:

```bash
docker compose -f compose.yml down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose -f compose.yml up --build --force-recreate
```

## RTMR3 Workload Policy

The live Phala path verifies the quote, certificate chain, QE identity and TCB status. It also fails closed unless the quote's dstack OS/boot tuple (`MRTD` and `RTMR0`--`RTMR2`) matches the owner-pinned tuple extracted during bootstrap from a reference dstack quote. That reference quote identifies the approved base runtime only; its Compose hash and `RTMR3` are not application allowlist inputs for the structured-log selector.

Registration supplies the exact canonical `app_compose` byte preimage reported by dstack, not a caller-supplied compose hash, image digest, or policy claim. `DeviceRegistry` parses `docker_compose_file` on-chain, requires the strict single-service `services.dfl-worker` form, extracts its immutable `@sha256:<digest>` value, and compares that digest with `expectedWorkerImageDigest`. It also derives a domain-separated `workerPolicyHash` over the image and the security-relevant service fields: entrypoint, command, user, mounts and dstack-socket access, capabilities, security options, networking, and environment safety.

Known per-instance environment keys such as the worker address, endpoints, round, shard inputs, and secret placeholders are normalized by key; their values remain bound by the complete Compose hash in `REPORTDATA` and RTMR3. Values of fixed or unknown environment keys are included in the policy hash, so an injected `NODE_OPTIONS` value changes the policy. Duplicate environment keys, unknown service-level fields, YAML aliases/merges, additional services, and top-level host-bind volume options fail closed. The owner provisions the two intended policies---training-only and Worker 0 with inference---before admission opens. This is deliberately narrower than pinning every complete worker Compose hash.

Only after the image and role policy pass does the Registry calculate `SHA-256` over the exact `app_compose` bytes. The worker also supplies the ordered RTMR3 event fields `(eventType, eventName, eventPayload)`. The verifier reconstructs every event digest on-chain as `SHA-384(LE32(eventType) || ":" || eventName || ":" || eventPayload)`, requires exactly one `compose-hash` event whose payload is the Registry-derived SHA-256 value, replays the RTMR3 extend chain, and compares the result with RTMR3 in the hardware-signed quote. No precomputed Compose or event digest is trusted as registration input.

The quote's 64-byte `REPORTDATA` has this format:

```text
bytes  0..31  keccak256(domain, deploymentId, chainId, registry,
                        verifier, device, endpoint hashes, public-key hash,
                        composeHash, imageDigest, workerPolicyHash, nonce)
bytes 32..63  uint256 registration nonce
```

The worker asks `DeviceRegistry.registrationReportData(...)` for these exact bytes before requesting its quote. Successful registration increments the per-device nonce. This binds the quote to the transaction sender, RSA key, endpoints, workload policy, Registry deployment and one registration attempt, while rejecting replay after the nonce changes. Registration is not controlled by an owner-managed address allowlist: any caller that satisfies the complete attestation and workload policy can register. The two legacy registration selectors always revert.

The local Docker flow cannot use one static quote for multiple dynamic worker identities. It therefore deploys `MockTdxV4Attestation` explicitly for Anvil, while exercising the same Registry binding and replay checks. This mock proves no hardware claim and the bootstrap refuses to enable it when `DOCKER=phala`.

For Phala/dstack this needs one subtle distinction:

- the RTMR3 `compose-hash` event is the SHA-256 of the normalized/canonical `app_compose` byte preimage; `phala/app_code.txt` can hold an exported copy for offline inspection
- the plain SHA-256 of `phala/dstack-compose.template.yml` is only the raw compose-file hash

No complete Compose-hash provisioning is required. Different endpoint-bearing worker Composes are accepted when their derived role policy is owner-approved, their image resolves to the configured digest, and their structured event log reconstructs the quote-bound RTMR3. Generated `app_code.txt` and Terraform state files are intentionally ignored and must not be committed.

## Generated Artifacts

Foundry generates build outputs in:

- `smart_contracts/out`
- `smart_contracts/cache`
- `smart_contracts/broadcast`
- `smart_contracts/lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs/out`
- `smart_contracts/lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs/cache`
- `smart_contracts/lib/automata-dcap-v3-attestation/lib/automata-on-chain-pccs/broadcast`

These directories are build/deployment artifacts, not source code.


## Other Readme´s
- [Neural Network README](../dfl/neural_network/README.md)
- [Node Server README](../dfl/node_server/README.md)
- [SMART Contracts README](./smart_contracts/README.md)
- [ZK Inference README](./zk_inference/README.md)
- [Agent README](./agent/README.md) 
