// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

import {Script} from "forge-std/Script.sol";
import {DstackKms} from "../vendor/dstack-v0.5.9/contracts/DstackKms.sol";

/// @notice Approve one explicitly checked KMS measurement/device and OS.
/// @dev Obtain these values from Onboard.GetAttestationInfo and check the
/// deployment and attestation independently before broadcasting. No wildcards.
contract AuthorizeOwnKms is Script {
    function run() external {
        DstackKms kms = DstackKms(vm.envAddress("KMS_AUTH_CONTRACT"));
        bytes32 mr = vm.envBytes32("KMS_AGGREGATED_MR");
        bytes32 device = vm.envBytes32("KMS_DEVICE_ID");
        bytes32 os = vm.envBytes32("KMS_OS_IMAGE_HASH");
        require(mr != bytes32(0) && device != bytes32(0) && os != bytes32(0), "explicit KMS trust anchors required");
        vm.startBroadcast(uint256(vm.envBytes32("ETH_WALLET_PRIVATE_KEY")));
        kms.addOsImageHash(os);
        kms.addKmsAggregatedMr(mr);
        kms.addKmsDevice(device);
        vm.stopBroadcast();
    }
}
