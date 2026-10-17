// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IAppAuth} from "./IAppAuth.sol";

interface IWorkerAdmissionPolicy {
    function validateKmsCompose(bytes calldata canonicalAppCompose) external view returns (bytes32, bytes32, bytes32);
    function expectedWorkerImageDigest() external view returns (bytes32);
    function allowedWorkerPolicyHashes(bytes32 policyHash) external view returns (bool);
}

/// @notice A participant may operate the CVM, but cannot broaden its key-release policy.
/// @dev No proxy, upgrade, ownership-transfer or allowlist mutation functions.
///      The KMS authenticates bootInfo through attestation BEFORE this view call.
contract WorkerAppAuth is IAppAuth {
    IWorkerAdmissionPolicy public immutable registry;
    address public immutable owner; // Deployment metadata; grants no policy powers.
    bytes32 public immutable composeHash;
    bytes32 public immutable imageDigest;
    bytes32 public immutable workerPolicyHash;

    constructor(address sharedRegistry, address operator, bytes memory compose) {
        registry = IWorkerAdmissionPolicy(sharedRegistry);
        owner = operator;
        (composeHash, imageDigest, workerPolicyHash) = registry.validateKmsCompose(compose);
    }

    function supportsInterface(bytes4 id) external pure returns (bool) {
        return id == 0x01ffc9a7 || id == 0x1e079198;
    }

    function allowedComposeHashes(bytes32 hash) external view returns (bool) { return hash == composeHash; }
    function allowedDeviceIds(bytes32) external pure returns (bool) { return true; }
    function requireTcbUpToDate() external pure returns (bool) { return true; }
    function allowAnyDevice() external pure returns (bool) { return true; }

    function isAppAllowed(AppBootInfo calldata bootInfo) external view returns (bool, string memory) {
        if (bootInfo.appId != address(this)) return (false, "app identity mismatch");
        if (bootInfo.composeHash != composeHash) return (false, "compose hash mismatch");
        if (keccak256(bytes(bootInfo.tcbStatus)) != keccak256("UpToDate")) return (false, "TCB not up to date");
        if (registry.expectedWorkerImageDigest() != imageDigest || !registry.allowedWorkerPolicyHashes(workerPolicyHash)) {
            return (false, "workload policy rejected");
        }
        return (true, "");
    }
}
