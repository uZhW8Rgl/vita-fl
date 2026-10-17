// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.20;

/// @notice ABI-compatible with dstack v0.5.9 IAppAuth (including ERC-165).
interface IAppAuth {
    struct AppBootInfo {
        address appId;
        bytes32 composeHash;
        address instanceId;
        bytes32 deviceId;
        bytes32 mrAggregated;
        bytes32 mrSystem;
        bytes32 osImageHash;
        string tcbStatus;
        string[] advisoryIds;
    }

    function supportsInterface(bytes4 interfaceId) external view returns (bool);
    function isAppAllowed(AppBootInfo calldata bootInfo) external view returns (bool, string memory);
}
