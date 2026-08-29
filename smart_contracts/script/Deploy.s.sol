// Copyright 2024 RISC Zero, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// SPDX-License-Identifier: Apache-2.0

pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {AggregatorSelection} from "../src/core/AggregatorSelection.sol";
import {GMStorage} from "../src/core/GMStorage.sol";
import {MedicalSignerRegistry} from "../src/core/MedicalSignerRegistry.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";



/// @notice Deployment script for the RISC Zero starter project.
/// @dev Use the following environment variable to control the deployment:
///     * ETH_WALLET_PRIVATE_KEY private key of the wallet to be used for deployment.
///

contract DeviceRegistryDeploy is Script {
    function run() external {
        uint256 deployerKey = uint256(vm.envBytes32("ETH_WALLET_PRIVATE_KEY"));
        bytes32 defaultDeploymentId = keccak256(
            abi.encode(
                "MasterThesis.DeviceRegistry.deployment.v1",
                block.chainid,
                block.timestamp,
                block.prevrandao,
                deployerKey
            )
        );
        bytes32 deploymentId = vm.envOr("DEVICE_REGISTRY_DEPLOYMENT_ID", defaultDeploymentId);

        vm.startBroadcast(deployerKey);

        DeviceRegistry deviceRegistry = new DeviceRegistry(deploymentId);
        console2.log("Deployed DeviceRegistry to", address(deviceRegistry));

        address initialAggregator = vm.envAddress("W0_ACCOUNT_ADDRESS");
        AggregatorSelection aggregatorSelection = new AggregatorSelection(
            initialAggregator
        );
        console2.log(
            "Deployed AggregatorSelection to",
            address(aggregatorSelection)
        );

        string memory initial_gm_cid = vm.envString("INITIAL_GM_CID");

        // needs deviceRegistry and AggrigatorSelection
        GMStorage gmStorage = new GMStorage(
            address(deviceRegistry),
            address(aggregatorSelection),
            initial_gm_cid
        );
        console2.log("Deployed GMStorage to", address(gmStorage));

        MedicalSignerRegistry medicalSignerRegistry = new MedicalSignerRegistry();
        console2.log("Deployed MedicalSignerRegistry to", address(medicalSignerRegistry));
        _configureMedicalSigners(medicalSignerRegistry);

        AggregationPolicy aggregationPolicy = new AggregationPolicy(address(gmStorage));
        console2.log("Deployed AggregationPolicy to", address(aggregationPolicy));
        gmStorage.setAggregationPolicyAddress(address(aggregationPolicy));

        uint256 requiredSubmissions = vm.envOr("CLIENT_LIMIT", uint256(2));
        uint256 submissionDeadlineMs = vm.envOr("MODEL_SUBMISSION_DEADLINE_MS", uint256(20_000));
        uint256 submissionWindowSeconds = (submissionDeadlineMs + 999) / 1000;
        require(requiredSubmissions <= type(uint32).max, "CLIENT_LIMIT exceeds uint32");
        require(submissionWindowSeconds <= type(uint64).max, "submission window exceeds uint64");
        aggregationPolicy.configureDefaultPolicy(
            uint32(requiredSubmissions),
            uint64(submissionWindowSeconds)
        );

        vm.stopBroadcast();
    }

    function _configureMedicalSigners(MedicalSignerRegistry registry) private {
        for (uint256 i = 0; i < 5; i++) {
            string memory prefix = string.concat("data/medical_signers/xray-device-", vm.toString(i));
            registry.configureSigner(
                bytes32(bytes(string.concat("XRAY_DEVICE_", vm.toString(i)))),
                MedicalSignerRegistry.SignerRole.XRAY_DEVICE,
                string.concat("Synthetic X-Ray Device ", vm.toString(i)),
                vm.readFileBinary(string.concat(prefix, "-public.der")),
                vm.readFileBinary(string.concat(prefix, "-certificate.der"))
            );
        }

        registry.configureSigner(
            bytes32("RADIOLOGIST_0"),
            MedicalSignerRegistry.SignerRole.RADIOLOGIST,
            "Synthetic Radiologist 0",
            vm.readFileBinary("data/medical_signers/radiologist-0-public.der"),
            vm.readFileBinary("data/medical_signers/radiologist-0-certificate.der")
        );
    }
}
