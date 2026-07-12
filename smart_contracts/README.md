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
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:64f0ac3d1ce001dc32e4bdf7cf1f39a0607a6a021e7b792c715f48fc52df7c29
```

Whenever the worker digest changes, or the files under `phala/` that feed the
runtime policy export change in a way that should be reflected inside the
contract-runtime TEE, rebuild and republish this image before redeploying the
runtime TEE.

It performs:

1. provider-specific initialization of the initial global model and signature,
2. deployment of `DeviceRegistry`, `AggregatorSelection`, and `GMStorage` with the locally imported model CIDs,
3. deployment/configuration of the Automata PCCS helper and DAO contracts,
4. deployment of `AutomataDcapTdxV4Attestation`,
5. authorization of the attestation contract as PCCS reader,
6. upload of PCCS collateral,
7. registration of initial global model metadata.

For the local Docker flow, `IPFS_PROVIDER` controls the bootstrap mode. With `IPFS_PROVIDER=kubo`, the deployment script signs `data/initial_gm/<dataset>/aggregated.bin`, imports model and signature into the local Kubo node, and writes those resulting CIDs into `GMStorage`. The dataset is selected through `DATASET_NAME` and defaults to `mnist`. With `IPFS_PROVIDER=pinata`, the script does not touch Kubo during initialization and instead expects `INITIAL_GM_CID` and `INITIAL_GM_SIG_CID` to already point to Pinata-hosted content.

The tested local entry point is:

```bash
docker compose -f compose.yml down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose -f compose.yml up --build --force-recreate
```

## RTMR3 Workload Policy

The live Phala path verifies the quote, certificate chain, QE identity and TCB status, then replays the submitted RTMR3 event chain. The exact `compose-hash` event must both occur in that replay and be owner-allowlisted. The replayed RTMR3 must equal the signed quote RTMR3.

`DeviceRegistry` additionally installs an owner-reviewed `composeHash -> imageDigest` mapping. A caller-supplied image digest without that exact mapping is rejected, so an arbitrary workload cannot merely claim the approved digest. Both the verifier and Registry policies are fail-closed when no approved compose hash is configured.

The quote's 64-byte `REPORTDATA` has this format:

```text
bytes  0..31  keccak256(domain, deploymentId, chainId, registry,
                        device, endpoint hashes, public-key hash,
                        composeHash, imageDigest, owner challenge,
                        challenge deadline, nonce)
bytes 32..63  uint256 registration nonce
```

The worker asks `DeviceRegistry.registrationReportData(...)` for these exact bytes before requesting its quote. The owner-issued challenge expires after one day and is consumed on success; successful registration also increments the nonce. This binds the quote to the transaction sender, RSA key, endpoints, workload policy, Registry deployment and one fresh registration attempt. The two legacy registration selectors always revert.

The local Docker flow cannot use one static quote for multiple dynamic worker identities. It therefore deploys `MockTdxV4Attestation` explicitly for Anvil, while exercising the same Registry binding and replay checks. This mock proves no hardware claim and the bootstrap refuses to enable it when `DOCKER=phala`.

For Phala/dstack this needs one subtle distinction:

- the RTMR3 `compose-hash` event is the SHA-256 of the normalized Phala app-code object from `phala/app_code.txt`
- the plain SHA-256 of `phala/dstack-compose.template.yml` is only the raw compose-file hash

Configure the first value, never the raw compose-file hash, in `PHALA_ALLOWED_WORKER_COMPOSE_HASHES`. If the image, endpoint-bearing compose, or another measured input changes, review and provision every new canonical hash before registration. Generated `app_code.txt` and Terraform state files are intentionally ignored and must not be committed.

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
