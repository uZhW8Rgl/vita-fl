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

        AggregatorSelection aggregatorSelection = new AggregatorSelection(
        );
        console2.log(
            "Deployed AggregatorSelection to",
            address(aggregatorSelection)
        );

        string memory initial_gm_cid = vm.envString("INITIAL_GM_CID");
        string memory initial_gm_sig_cid = vm.envString("INITIAL_GM_SIG_CID");
        address initial_gm_signer_address = vm.envAddress("INITIAL_GM_SIGNER_ADDRESS");

        // needs deviceRegistry and AggrigatorSelection
        GMStorage gmStorage = new GMStorage(
            address(deviceRegistry),
            address(aggregatorSelection),
            initial_gm_cid,
            initial_gm_sig_cid,
            initial_gm_signer_address
        );
        console2.log("Deployed GMStorage to", address(gmStorage));

        vm.stopBroadcast();
    }
}
