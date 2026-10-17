// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {WorkerAppAuthFactory} from "../src/kms/WorkerAppAuthFactory.sol";

/// @notice Configure shared KMS trust before the first successful enrollment.
/// @dev Run on the SAME chain as the selected KMS policy, including an own
/// dstack KMS on Anvil. Hosted Phala KMS does not consult this Anvil policy.
contract ConfigureKms is Script {
    function run() external {
        address registryAddress = vm.envAddress("REGISTRY_ADDRESS");
        address kmsAddress = vm.envAddress("KMS_AUTH_CONTRACT");
        bytes32 providerHash = vm.envBytes32("KMS_KEY_PROVIDER_HASH");
        uint256 deployer = uint256(vm.envBytes32("ETH_WALLET_PRIVATE_KEY"));
        vm.startBroadcast(deployer);
        DeviceRegistry registry = DeviceRegistry(registryAddress);
        WorkerAppAuthFactory factory = new WorkerAppAuthFactory(registryAddress, kmsAddress);
        registry.setKmsAppFactory(address(factory), providerHash);
        vm.stopBroadcast();
        console2.log("Shared worker AppAuth factory:", address(factory));
    }
}
