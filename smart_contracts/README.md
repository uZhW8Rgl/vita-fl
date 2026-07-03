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
ghcr.io/uzhw8rgl/master-thesis-smart-contracts@sha256:<digest>
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

The local prototype verifies the TDX/DCAP quote, certificate chain, QE identity, and TCB status. In addition, the deployment script configures a workload policy from the checked-in reference quote at `data/phala_tdx_quote`.

During deployment, `starter_docker.sh` calls:

```text
setExpectedRtmr3FromQuote(bytes referenceQuote)
```

The contract parses the reference quote on-chain, extracts RTMR3, and rejects every later worker registration quote whose extracted RTMR3 does not match. `starter_docker.sh` also checks that the worker compose policy pins the image by immutable `sha256` digest and records the measured compose policy hash on-chain as `expectedComposeHash`.

For Phala/dstack this needs one subtle distinction:

- the RTMR3 `compose-hash` event is the SHA-256 of the normalized Phala app-code object from `phala/app_code.txt`
- the plain SHA-256 of `phala/dstack-compose.template.yml` is only the raw compose-file hash

`starter_docker.sh` therefore prefers `phala/app_code.txt` when present and only falls back to the raw compose-file hash if no app-code export is available.

The compose policy is still the replaceable workload input. If the worker image changes, update the digest-pinned image reference in `phala/dstack-compose.template.yml`, deploy that exact worker app on Phala/dstack, fetch the new quote/app-code/event log, and rerun the local deployment.

A complete image-to-RTMR3 verification additionally requires the Phala/dstack RTMR3 event log. With that log, a verifier can replay the RTMR3 measurement chain, compare the measured compose hash with `expectedComposeHash`, and then compare the replayed RTMR3 with the signed RTMR3 in the quote.

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
