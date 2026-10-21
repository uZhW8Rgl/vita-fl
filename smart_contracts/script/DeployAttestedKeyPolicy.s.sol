// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;
import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {AttestedKeyPolicy} from "../src/keys/AttestedKeyPolicy.sol";
contract DeployAttestedKeyPolicy is Script {
    function run() external {
        require(block.chainid == 31337, "Anvil only; no public-chain transactions");
        uint256 key = uint256(vm.envBytes32("KEY_POLICY_ADMIN_PRIVATE_KEY"));
        vm.startBroadcast(key);
        AttestedKeyPolicy policy = new AttestedKeyPolicy(vm.envAddress("REGISTRY_ADDRESS"));
        vm.stopBroadcast();
        console2.log("KEY_SERVICE_POLICY", address(policy));
        console2.log("KEY_SERVICE_POLICY_CODE_HASH");
        console2.logBytes32(address(policy).codehash);
    }
}
