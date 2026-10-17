// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {WorkerAppAuth} from "./WorkerAppAuth.sol";

interface IDstackKmsRegistry { function registerApp(address appId) external; }

/// @notice Automatic per-app KMS approval from the shared workload policy.
/// @dev Pre-boot creation does not grant DFL membership. Live DCAP enrollment does.
contract WorkerAppAuthFactory {
    address public immutable registry;
    IDstackKmsRegistry public immutable kms;
    mapping(address => bytes32) public appComposeHashes;
    mapping(address => address) public appOperators;

    event WorkerAppCreated(address indexed operator, address indexed appId, bytes32 composeHash);

    constructor(address sharedRegistry, address kmsRegistry) {
        require(sharedRegistry.code.length != 0 && kmsRegistry.code.length != 0, "invalid policy contracts");
        registry = sharedRegistry;
        kms = IDstackKmsRegistry(kmsRegistry);
    }

    function createApp(bytes calldata canonicalCompose, bytes32 salt) external returns (address appId) {
        bytes32 scopedSalt = keccak256(abi.encode(msg.sender, salt));
        WorkerAppAuth app = new WorkerAppAuth{salt: scopedSalt}(registry, msg.sender, canonicalCompose);
        appId = address(app);
        appComposeHashes[appId] = app.composeHash();
        appOperators[appId] = msg.sender;
        kms.registerApp(appId);
        emit WorkerAppCreated(msg.sender, appId, app.composeHash());
    }

    function isApprovedApp(address appId, bytes32 composeHash) external view returns (bool) {
        return appOperators[appId] != address(0) && appComposeHashes[appId] == composeHash;
    }
}
