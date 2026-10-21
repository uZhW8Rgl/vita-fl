// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

/// @notice Local key-release policy. No keys, mainnet bridge, or Phala app-ID provisioning.
/// @dev The service MUST verify attestation and participant delegation before calling this view.
/// Anvil and the independent policy administrator are trusted in this development profile.
contract AttestedKeyPolicy {
    address public immutable administrator;
    address public immutable registry;
    struct Grant { bytes32 composeHash; bool revoked; }
    mapping(address => mapping(address => Grant)) public grants;
    event Authorised(address indexed participant, address indexed appId, bytes32 composeHash);
    event Revoked(address indexed participant, address indexed appId);

    constructor(address registryAddress) {
        require(block.chainid == 31337, "Anvil chain required");
        require(registryAddress != address(0), "registry required");
        administrator = msg.sender;
        registry = registryAddress;
    }
    modifier onlyAdministrator() {
        require(msg.sender == administrator, "administrator required"); _;
    }
    function authorise(address participant, address appId, bytes32 composeHash) external onlyAdministrator {
        require(participant != address(0) && appId != address(0) && composeHash != bytes32(0), "invalid grant");
        require(grants[participant][appId].composeHash == bytes32(0), "binding is immutable");
        grants[participant][appId] = Grant(composeHash, false);
        emit Authorised(participant, appId, composeHash);
    }
    function revoke(address participant, address appId) external onlyAdministrator {
        require(grants[participant][appId].composeHash != bytes32(0), "unknown grant");
        grants[participant][appId].revoked = true;
        emit Revoked(participant, appId);
    }
    function isAllowed(address participant, address appId, bytes32 composeHash) external view returns (bool) {
        Grant memory grant = grants[participant][appId];
        return composeHash != bytes32(0) && grant.composeHash == composeHash && !grant.revoked;
    }
}
