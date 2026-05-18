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

It performs:

1. deployment of `DeviceRegistry`, `AggregatorSelection`, and `GMStorage`,
2. deployment/configuration of the Automata PCCS helper and DAO contracts,
3. deployment of `AutomataDcapTdxV4Attestation`,
4. authorization of the attestation contract as PCCS reader,
5. upload of PCCS collateral,
6. registration of initial global model metadata.

The tested local entry point is:

```bash
docker compose -f compose.yml down --volumes --remove-orphans
KEEP_ALIVE=0 docker compose -f compose.yml up --build --force-recreate
```

## Optional RTMR3 Workload Policy

By default, the local prototype verifies the TDX/DCAP quote, certificate chain, QE identity, and TCB status while keeping the worker registration flow usable with the checked-in local quote at `data/phala_tdx_quote`.

For a stricter Phala/dstack workload policy, set `EXPECTED_TDX_RTMR3` to the 48-byte RTMR3 value from the intended deployment quote:

```bash
EXPECTED_TDX_RTMR3=0x<96 hex chars>
```

When this value is set, `AutomataDcapTdxV4Attestation` rejects every quote whose extracted RTMR3 does not match the configured value. This allows the local flow to keep using mock/demo quotes when the value is empty, while a real Phala deployment can bind worker registration to the expected application measurement. Docker image verification is therefore represented through the expected RTMR3 policy: the deployed compose file should pin images by immutable `sha256` digests, and the resulting Phala/dstack RTMR3 value should be configured as `EXPECTED_TDX_RTMR3`.

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
