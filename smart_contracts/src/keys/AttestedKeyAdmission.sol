// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

import {AttestedKeyPolicy} from "./AttestedKeyPolicy.sol";
import {StrictECDSA} from "../crypto/StrictECDSA.sol";
import {Rtmr3Event} from "../attestation/Rtmr3Event.sol";

interface IAdmissionRegistry {
    function validateKmsCompose(bytes calldata app) external view returns (bytes32, bytes32, bytes32);
    function tdxV4Attestation() external view returns (address);
}
interface IAdmissionVerifier {
    function verifyAndAttestOnChainWithRtmr3EventLog(bytes calldata quote, Rtmr3Event[] calldata events,
        bytes32 composeHash) external view returns (bytes memory);
}

/// @notice One DCAP appraisal authorises public identity and one recipient-bound key release.
/// @dev Anvil/registry/policy administrator are trusted. This stores no private keys.
contract AttestedKeyAdmission is AttestedKeyPolicy {
    using StrictECDSA for bytes32;
    bytes32 public immutable serviceId;
    bytes32 public constant DOMAIN = keccak256("VITA-FL.attested-key-admission.v2");
    struct Identity { address actionKey; bytes rsaPublicKey; bytes32 selloKey; bytes32 airKey; }
    struct Request {
        address participant; address appId; bytes32 composeHash; bytes32 challenge;
        bytes32 recipientPublicKey; uint64 expiresAt; bool restoring;
    }
    struct Permit { bytes32 binding; uint64 expiresAt; }
    mapping(address => mapping(address => Identity)) private identities;
    mapping(bytes32 => Permit) public permits;
    event IdentityAdmitted(address indexed participant, address indexed appId, bytes32 identityHash);
    event ReleaseAuthorised(bytes32 indexed challenge, address indexed participant, bytes32 binding);

    constructor(address registryAddress, bytes32 id) AttestedKeyPolicy(registryAddress) {
        require(id != bytes32(0), "service id required"); serviceId = id;
    }
    function identityFor(address participant, address appId) external view returns (Identity memory) {
        return identities[participant][appId];
    }
    function identityHash(Identity memory identity) public pure returns (bytes32) {
        return keccak256(abi.encode(identity));
    }
    function reportData(Request memory request, Identity memory identity) public view returns (bytes memory) {
        return abi.encodePacked(keccak256(abi.encode(DOMAIN, block.chainid, address(this), registry,
            serviceId, request, identityHash(identity))), request.challenge);
    }
    function authorisationDigest(Request memory request, Identity memory identity) public view returns (bytes32) {
        return keccak256(reportData(request, identity));
    }
    function admit(Request calldata request, Identity calldata identity, bytes calldata participantSignature,
        bytes calldata quote, Rtmr3Event[] calldata events, bytes calldata appCompose) external {
        require(request.expiresAt > block.timestamp && request.expiresAt <= block.timestamp + 300, "release expired");
        require(request.challenge != bytes32(0) && request.recipientPublicKey != bytes32(0), "empty release context");
        require(permits[request.challenge].binding == bytes32(0), "challenge already used");
        require(this.isAllowed(request.participant, request.appId, request.composeHash), "workload policy rejected");
        require(identity.actionKey != address(0) && identity.rsaPublicKey.length >= 384 &&
            identity.rsaPublicKey.length <= 1024 && identity.selloKey != bytes32(0) && identity.airKey != bytes32(0),
            "invalid public identity");
        bytes32 digest = authorisationDigest(request, identity);
        require(keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", digest))
            .recover(participantSignature) == request.participant, "participant authorisation rejected");
        Identity storage stored = identities[request.participant][request.appId];
        if (request.restoring) {
            require(stored.actionKey != address(0) && identityHash(stored) == identityHash(identity), "identity restore mismatch");
        } else require(stored.actionKey == address(0), "identity already exists");
        (bytes32 composeHash,,) = IAdmissionRegistry(registry).validateKmsCompose(appCompose);
        require(composeHash == request.composeHash, "manifest mismatch");
        bool seen;
        for (uint256 i; i < events.length; ++i) {
            if (keccak256(bytes(events[i].eventName)) == keccak256("app-id")) {
                require(!seen && events[i].eventPayload.length == 20, "invalid app id event"); seen = true;
                require(keccak256(events[i].eventPayload) == keccak256(abi.encodePacked(request.appId)), "app id mismatch");
            }
        }
        require(seen, "app id missing");
        bytes memory report = IAdmissionVerifier(IAdmissionRegistry(registry).tdxV4Attestation())
            .verifyAndAttestOnChainWithRtmr3EventLog(quote, events, composeHash);
        require(keccak256(report) == keccak256(reportData(request, identity)), "quote report data mismatch");
        // No secret release or identity write is possible before the one real DCAP appraisal above.
        if (!request.restoring) {
            identities[request.participant][request.appId] = identity;
            emit IdentityAdmitted(request.participant, request.appId, identityHash(identity));
        }
        permits[request.challenge] = Permit(digest, request.expiresAt);
        emit ReleaseAuthorised(request.challenge, request.participant, digest);
    }
    function releaseAuthorised(Request calldata request, Identity calldata identity) external view returns (bool) {
        Permit memory permit = permits[request.challenge];
        return permit.expiresAt > block.timestamp && permit.binding == authorisationDigest(request, identity) &&
            this.isAllowed(request.participant, request.appId, request.composeHash) &&
            identityHash(identities[request.participant][request.appId]) == identityHash(identity);
    }
    function isIdentityAdmitted(address participant, address appId, bytes32 composeHash, address actionKey,
        bytes calldata rsaPublicKey, bytes32 selloKey) external view returns (bool) {
        Identity storage stored = identities[participant][appId];
        return this.isAllowed(participant, appId, composeHash) && stored.actionKey == actionKey &&
            keccak256(stored.rsaPublicKey) == keccak256(rsaPublicKey) && stored.selloKey == selloKey;
    }
}
