// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;
import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {AttestedKeyAdmission} from "../src/keys/AttestedKeyAdmission.sol";

/// Optional admission deployment within the ordinary ephemeral Foundry bootstrap.
contract DeployAttestedKeyAdmission is Script {
    function run() external {
        require(block.chainid == 31337, "Anvil only; no public-chain transactions");
        DeviceRegistry registry = DeviceRegistry(vm.envAddress("DEVICE_REGISTRY_ADDRESS"));
        // Public context identifier, never a secret or a source of worker roots.
        bytes32 serviceId = keccak256(abi.encode("VITA-FL admission v2", block.chainid, address(registry), registry.deploymentId()));
        vm.startBroadcast(uint256(vm.envBytes32("ETH_WALLET_PRIVATE_KEY")));
        AttestedKeyAdmission policy = new AttestedKeyAdmission(address(registry), serviceId);
        registry.setKeyAdmissionPolicy(address(policy));
        vm.stopBroadcast();
        console2.log("KEY_SERVICE_POLICY", address(policy));
        console2.log("KEY_SERVICE_ID");
        console2.logBytes32(serviceId);
    }
}
