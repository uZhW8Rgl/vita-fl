// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {DstackKms} from "../vendor/dstack-v0.5.9/contracts/DstackKms.sol";
import {ERC1967Proxy} from "../vendor/openzeppelin-5.4.0/contracts/proxy/ERC1967/ERC1967Proxy.sol";

/// @notice Deploy the original dstack KMS policy on the registry's chain.
/// @dev This deploys policy, NOT a secret-holding KMS service. Root/OS/KMS
/// measurements must be approved separately before the real KMS can bootstrap.
contract DeployOwnKms is Script {
    function run() external {
        bytes32 osImageHash = vm.envBytes32("KMS_OS_IMAGE_HASH");
        uint256 key = uint256(vm.envBytes32("ETH_WALLET_PRIVATE_KEY"));
        require(osImageHash != bytes32(0), "explicit OS trust configuration required");
        vm.startBroadcast(key);
        DstackKms implementation = new DstackKms();
        // No generic, operator-mutable DstackApp implementation is offered.
        DstackKms kms = DstackKms(address(new ERC1967Proxy(address(implementation),
            abi.encodeCall(DstackKms.initialize, (vm.addr(key), address(0))))));
        kms.addOsImageHash(osImageHash);
        vm.stopBroadcast();
        console2.log("KMS policy:", address(kms));
        console2.log("Next: authorize the KMS measurement/device, bootstrap, then ConfigureKms with its measured key-provider hash.");
    }
}
