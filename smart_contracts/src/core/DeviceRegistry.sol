// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IAppComposePolicy, AppComposePolicy} from "../attestation/AppComposeImage.sol";
import {Rtmr3Event} from "../attestation/Rtmr3Event.sol";
import {StrictECDSA} from "../crypto/StrictECDSA.sol";

interface ITdxV4Attestation {
    function verifyAndAttestOnChainWithRtmr3EventLog(
        bytes calldata input,
        Rtmr3Event[] calldata rtmr3EventLog,
        bytes32 composeHash
    ) external view returns (bytes memory output);
}

contract DeviceRegistry {
    using StrictECDSA for bytes32;

    struct Device {
        bool authorized;
        string public_ip;
        string msg_broker_ip;
        bytes public_key;
    }

    uint256 private constant REPORT_DATA_LENGTH = 64;

    bytes32 public constant REGISTRATION_REPORT_DATA_DOMAIN = keccak256("MasterThesis.DeviceRegistry.registration.v6");
    bytes32 public constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant ENROLLMENT_TYPEHASH = keccak256(
        "Enrollment(address participant,address actionKey,bytes32 composeHash,bytes32 selloReceiptKey,uint256 nonce)"
    );
    bytes32 private constant EIP712_NAME_HASH = keccak256("VITA-FL DeviceRegistry");
    bytes32 private constant EIP712_VERSION_HASH = keccak256("6");

    address public owner;
    bytes32 public immutable deploymentId;
    ITdxV4Attestation public tdxV4Attestation;
    IAppComposePolicy public immutable appComposePolicy;
    bytes32 public expectedWorkerImageDigest;
    mapping(address => Device) public devices;
    mapping(address => uint256) public registrationNonces;
    mapping(address => bytes32) public registeredComposeHashes;
    mapping(address => bytes32) public registeredImageDigests;
    mapping(address => bytes32) public registeredWorkerPolicyHashes;
    mapping(address => bytes32) public selloReceiptKeys;
    mapping(address => address) public actionKeyForParticipant;
    mapping(address => address) public participantForActionKey;
    mapping(bytes32 => bool) public allowedWorkerPolicyHashes;
    uint256 public allowedWorkerPolicyHashCount;
    mapping(address => bool) public committedRunMembers;
    address[] private committedRunRoster;
    bytes32 public runRosterDigest;
    bool public runRosterCommitted;
    bool public runRosterFrozen;
    uint256 public registeredRunMemberCount;
    mapping(address => bool) private knownDevice;
    address[] private deviceAddresses;
    uint256 public number;

    event DeviceRegistered(address indexed device, uint256 indexed registrationNonce, bytes32 indexed composeHash);
    event DeviceAuthorized(address indexed device);
    event DeviceDeauthorized(address indexed device);
    event DeviceLeft(address indexed device);
    event DeviceDeregistered(address indexed device, uint256 registrationNonce);
    event ExpectedWorkerImageDigestUpdated(bytes32 expectedWorkerImageDigest);
    event WorkerPolicyHashPermissionUpdated(bytes32 indexed workerPolicyHash, bool allowed);
    event DeviceWorkerPolicyRegistered(address indexed device, bytes32 indexed workerPolicyHash);
    event DeviceActionKeyRegistered(address indexed device, address indexed actionKey);
    event DeviceSelloReceiptKeyRegistered(address indexed device, bytes32 indexed selloReceiptKey);
    event RunRosterCommitted(bytes32 indexed rosterDigest, uint256 workerCount, address indexed bootstrapWorker);
    event RunRosterFrozen(bytes32 indexed rosterDigest, uint256 workerCount);

    constructor(bytes32 _deploymentId) {
        require(_deploymentId != bytes32(0), "invalid deployment id");
        owner = msg.sender;
        deploymentId = _deploymentId;
        appComposePolicy = new AppComposePolicy();
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function setTdxV4Attestation(address _tdxV4Attestation) public onlyOwner {
        require(!runRosterCommitted, "run admission policy is immutable");
        require(_tdxV4Attestation != address(0), "invalid attestation address");
        tdxV4Attestation = ITdxV4Attestation(_tdxV4Attestation);
    }

    function setExpectedWorkerImageDigest(bytes32 _expectedWorkerImageDigest) public onlyOwner {
        require(!runRosterCommitted, "run admission policy is immutable");
        expectedWorkerImageDigest = _expectedWorkerImageDigest;
        emit ExpectedWorkerImageDigestUpdated(_expectedWorkerImageDigest);
    }

    /// @notice Adds or removes one role-specific worker policy derived from selected
    ///         security fields. This is not an allowlist of complete Compose hashes:
    ///         worker-specific environment, endpoints and other runtime values remain
    ///         free to vary and are bound separately through REPORTDATA and RTMR3.
    function setWorkerPolicyHashAllowed(bytes32 policyHash, bool allowed) public onlyOwner {
        require(!runRosterCommitted, "run admission policy is immutable");
        require(policyHash != bytes32(0), "invalid worker policy hash");
        bool current = allowedWorkerPolicyHashes[policyHash];
        if (current == allowed) return;

        allowedWorkerPolicyHashes[policyHash] = allowed;
        if (allowed) allowedWorkerPolicyHashCount++;
        else allowedWorkerPolicyHashCount--;
        emit WorkerPolicyHashPermissionUpdated(policyHash, allowed);
    }

    /// @notice Commits the exact, ordered participant set for this deployment.
    /// @dev This is deliberately a one-time owner action. Registration remains
    ///      closed until the commitment exists, and only committed participants
    ///      can subsequently register attested workload/action keys.
    function commitRunRoster(address[] calldata roster) external onlyOwner {
        require(!runRosterCommitted, "run roster already committed");
        require(roster.length > 0, "run roster is empty");

        for (uint256 i = 0; i < roster.length; i++) {
            address participant = roster[i];
            require(participant != address(0), "run roster contains zero address");
            require(!knownDevice[participant], "run member already registered");
            require(!committedRunMembers[participant], "run roster contains duplicate");
            committedRunMembers[participant] = true;
            committedRunRoster.push(participant);
        }

        runRosterDigest = keccak256(abi.encode(roster));
        runRosterCommitted = true;
        emit RunRosterCommitted(runRosterDigest, roster.length, roster[0]);
    }

    function getRunRoster() external view returns (address[] memory) {
        return committedRunRoster;
    }

    function runRosterSize() external view returns (uint256) {
        return committedRunRoster.length;
    }

    function authorizeAddress(address _address) public onlyOwner {
        require(!runRosterCommitted, "run roster is immutable");
        require(knownDevice[_address], "device not registered");
        devices[_address].authorized = true;
        emit DeviceAuthorized(_address);
    }

    function deauthorizeAddress(address _address) public onlyOwner {
        require(!runRosterCommitted, "run roster is immutable");
        require(knownDevice[_address], "device not registered");
        devices[_address].authorized = false;
        emit DeviceDeauthorized(_address);
    }

    function leaveNetwork() public {
        address participant = _participantForCurrentAction(msg.sender);
        _deregisterDevice(participant);
        emit DeviceLeft(participant);
    }

    /// @notice Removes the caller's own verified registration and workload identity.
    /// @dev The registration nonce is intentionally retained to prevent replay of an old quote.
    function deregisterDevice() external {
        _deregisterDevice(_participantForCurrentAction(msg.sender));
    }

    function isAuthorized(address _address) public view returns (bool) {
        if (
            !runRosterCommitted || !committedRunMembers[_address] || !devices[_address].authorized
                || expectedWorkerImageDigest == bytes32(0)
        ) {
            return false;
        }
        bytes32 imageDigest = registeredImageDigests[_address];
        bytes32 policyHash = registeredWorkerPolicyHashes[_address];
        return imageDigest == expectedWorkerImageDigest && allowedWorkerPolicyHashes[policyHash];
    }

    function isDeviceRegistrationCurrent(
        address _address,
        address _actionKey,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 selloReceiptKey,
        bytes memory canonicalAppCompose
    ) external view returns (bool) {
        (bytes32 composeHash, bytes32 imageDigest, bytes32 policyHash) = _workloadIdentity(canonicalAppCompose);
        return isAuthorized(_address) && actionKeyForParticipant[_address] == _actionKey
            && participantForActionKey[_actionKey] == _address && registeredComposeHashes[_address] == composeHash
            && registeredImageDigests[_address] == imageDigest && registeredWorkerPolicyHashes[_address] == policyHash
            && selloReceiptKeys[_address] == selloReceiptKey
            && keccak256(bytes(devices[_address].public_ip)) == keccak256(bytes(_public_ip))
            && keccak256(bytes(devices[_address].msg_broker_ip)) == keccak256(bytes(_msg_broker_ip))
            && keccak256(devices[_address].public_key) == keccak256(_public_key);
    }

    function getAuthorizedDevices() external view returns (address[] memory) {
        uint256 count = 0;
        for (uint256 i = 0; i < committedRunRoster.length; i++) {
            if (isAuthorized(committedRunRoster[i])) {
                count++;
            }
        }

        address[] memory authorized = new address[](count);
        uint256 index = 0;
        for (uint256 i = 0; i < committedRunRoster.length; i++) {
            address device = committedRunRoster[i];
            if (isAuthorized(device)) {
                authorized[index] = device;
                index++;
            }
        }
        return authorized;
    }

    function getDevice(address _address) external view returns (bool, string memory, string memory, bytes memory) {
        return (
            isAuthorized(_address),
            devices[_address].public_ip,
            devices[_address].msg_broker_ip,
            devices[_address].public_key
        );
    }

    /// @notice Resolves the admission-attested Sello receiver endpoint and receipt key.
    /// @dev The receiver is fixed to the bootstrap worker (roster[0]). Resolution
    ///      deliberately reverts until the complete roster is frozen and W0 remains
    ///      authorized, so callers cannot silently fall back to an unbound key.
    function getSelloReceiver(address participant)
        external
        view
        returns (bool authorized, string memory publicIp, bytes32 selloReceiptKey)
    {
        require(runRosterFrozen, "run roster not frozen");
        require(committedRunRoster.length != 0 && participant == committedRunRoster[0], "not bootstrap worker");
        require(isAuthorized(participant), "bootstrap worker not authorized");
        selloReceiptKey = selloReceiptKeys[participant];
        require(selloReceiptKey != bytes32(0), "sello receiver key unavailable");
        return (true, devices[participant].public_ip, selloReceiptKey);
    }

    /// @notice Resolves a transaction signer to its current logical participant.
    /// @dev All protocol contracts use this fail-closed resolver instead of treating
    ///      the publicly known participant account as operational authority.
    function resolveAuthorizedParticipant(address actionKey) external view returns (address participant) {
        participant = participantForActionKey[actionKey];
        require(participant != address(0), "action key not registered");
        require(actionKeyForParticipant[participant] == actionKey, "action key is not current");
        require(isAuthorized(participant), "participant is not authorized");
    }

    function actionKeys(address participant) external view returns (address) {
        return actionKeyForParticipant[participant];
    }

    /// @notice Disabled because this legacy selector cannot bind a quote to the caller, key, nonce and workload.
    function registerDevice(bytes calldata, address, string memory, string memory, bytes memory) public pure {
        revert("bound registration required");
    }

    /// @notice Disabled because this legacy selector has no image-to-compose policy argument.
    function registerDeviceWithRtmr3Events(
        bytes calldata,
        bytes[] calldata,
        bytes32,
        address,
        string memory,
        string memory,
        bytes memory
    ) public pure {
        revert("bound registration required");
    }

    function registerDeviceWithRtmr3EventsAndImageDigest(
        bytes calldata,
        bytes[] calldata,
        bytes32,
        bytes32,
        address,
        string memory,
        string memory,
        bytes memory
    ) public pure {
        revert("app compose evidence required");
    }

    function registerDeviceWithAttestedAppCompose(
        bytes calldata quote,
        Rtmr3Event[] calldata rtmr3EventLog,
        bytes calldata canonicalAppCompose,
        address _address,
        address actionKey,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 selloReceiptKey,
        bytes memory participantAuthorization
    ) public {
        require(actionKey == msg.sender, "sender/action key mismatch");
        require(!knownDevice[_address], "device already registered");

        // Image and role policy are derived from the exact app_compose preimage on-chain.
        // The complete preimage is then hashed independently for REPORTDATA and RTMR3 replay.
        (bytes32 composeHash, bytes32 workerImageDigest, bytes32 policyHash) = _workloadIdentity(canonicalAppCompose);
        _requireRegistrationPreparation(_address, _public_key, workerImageDigest, policyHash, selloReceiptKey);

        uint256 nonce = registrationNonces[_address];
        require(actionKey != address(0), "invalid action key");
        require(
            _enrollmentDigest(_address, actionKey, composeHash, selloReceiptKey, nonce)
                    .recover(participantAuthorization) == _address,
            "invalid participant authorization"
        );
        address assignedParticipant = participantForActionKey[actionKey];
        require(assignedParticipant == address(0), "action key already assigned");
        bytes32 binding = _registrationBinding(
            _address,
            actionKey,
            _public_ip,
            _msg_broker_ip,
            _public_key,
            composeHash,
            workerImageDigest,
            policyHash,
            selloReceiptKey,
            nonce
        );
        bytes memory output =
            tdxV4Attestation.verifyAndAttestOnChainWithRtmr3EventLog(quote, rtmr3EventLog, composeHash);
        _requireBoundReportData(output, binding, nonce);

        registrationNonces[_address] = nonce + 1;
        registeredComposeHashes[_address] = composeHash;
        registeredImageDigests[_address] = workerImageDigest;
        registeredWorkerPolicyHashes[_address] = policyHash;
        selloReceiptKeys[_address] = selloReceiptKey;
        actionKeyForParticipant[_address] = actionKey;
        participantForActionKey[actionKey] = _address;
        _registerVerifiedDevice(_address, _public_ip, _msg_broker_ip, _public_key);
        emit DeviceRegistered(_address, nonce, composeHash);
        emit DeviceWorkerPolicyRegistered(_address, policyHash);
        emit DeviceActionKeyRegistered(_address, actionKey);
        if (selloReceiptKey != bytes32(0)) {
            emit DeviceSelloReceiptKeyRegistered(_address, selloReceiptKey);
        }
    }

    /// @notice Returns the exact 64 bytes that the TEE must place into TDREPORT.REPORTDATA.
    function registrationReportData(
        address _address,
        address actionKey,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 selloReceiptKey,
        bytes memory canonicalAppCompose
    ) public view returns (bytes memory) {
        require(actionKey != address(0), "invalid action key");
        require(!knownDevice[_address], "device already registered");
        (bytes32 composeHash, bytes32 workerImageDigest, bytes32 policyHash) = _workloadIdentity(canonicalAppCompose);
        _requireRegistrationPreparation(_address, _public_key, workerImageDigest, policyHash, selloReceiptKey);
        uint256 nonce = registrationNonces[_address];
        bytes32 binding = _registrationBinding(
            _address,
            actionKey,
            _public_ip,
            _msg_broker_ip,
            _public_key,
            composeHash,
            workerImageDigest,
            policyHash,
            selloReceiptKey,
            nonce
        );
        return abi.encodePacked(binding, bytes32(nonce));
    }

    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(EIP712_DOMAIN_TYPEHASH, EIP712_NAME_HASH, EIP712_VERSION_HASH, block.chainid, address(this))
        );
    }

    /// @notice Digest signed by the logical participant to delegate registration
    ///         and subsequent protocol authority to one TEE-derived action key.
    function enrollmentDigest(
        address participant,
        address actionKey,
        bytes32 selloReceiptKey,
        bytes memory canonicalAppCompose
    ) public view returns (bytes32) {
        return _enrollmentDigest(
            participant, actionKey, sha256(canonicalAppCompose), selloReceiptKey, registrationNonces[participant]
        );
    }

    function _enrollmentDigest(
        address participant,
        address actionKey,
        bytes32 composeHash,
        bytes32 selloReceiptKey,
        uint256 nonce
    ) internal view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(ENROLLMENT_TYPEHASH, participant, actionKey, composeHash, selloReceiptKey, nonce)
        );
        return keccak256(abi.encodePacked(hex"1901", domainSeparator(), structHash));
    }

    function workloadIdentity(bytes memory canonicalAppCompose)
        external
        view
        returns (bytes32 composeHash, bytes32 workerImageDigest)
    {
        (composeHash, workerImageDigest,) = _workloadIdentity(canonicalAppCompose);
    }

    function workerPolicyHash(bytes memory canonicalAppCompose) external view returns (bytes32) {
        return appComposePolicy.workerPolicyHash(canonicalAppCompose);
    }

    function _workloadIdentity(bytes memory canonicalAppCompose)
        internal
        view
        returns (bytes32 composeHash, bytes32 workerImageDigest, bytes32 policyHash)
    {
        (workerImageDigest, policyHash) = appComposePolicy.identity(canonicalAppCompose);
        composeHash = sha256(canonicalAppCompose);
    }

    function _registrationBinding(
        address _address,
        address actionKey,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 composeHash,
        bytes32 workerImageDigest,
        bytes32 policyHash,
        bytes32 selloReceiptKey,
        uint256 nonce
    ) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                REGISTRATION_REPORT_DATA_DOMAIN,
                deploymentId,
                block.chainid,
                address(this),
                address(tdxV4Attestation),
                _address,
                actionKey,
                keccak256(bytes(_public_ip)),
                keccak256(bytes(_msg_broker_ip)),
                keccak256(_public_key),
                composeHash,
                workerImageDigest,
                policyHash,
                selloReceiptKey,
                nonce
            )
        );
    }

    function _requireRegistrationPreparation(
        address _address,
        bytes memory _public_key,
        bytes32 workerImageDigest,
        bytes32 policyHash,
        bytes32 selloReceiptKey
    ) internal view {
        require(_address != address(0), "invalid device address");
        require(runRosterCommitted, "run roster not committed");
        require(committedRunMembers[_address], "participant not in committed run roster");
        require(!runRosterFrozen, "run roster registration is closed");
        if (_address == committedRunRoster[0]) {
            require(selloReceiptKey != bytes32(0), "bootstrap sello receiver key required");
        } else {
            require(selloReceiptKey == bytes32(0), "non-bootstrap sello receiver key forbidden");
        }
        require(_public_key.length != 0, "public key required");
        require(expectedWorkerImageDigest != bytes32(0), "worker image policy not configured");
        require(workerImageDigest == expectedWorkerImageDigest, "worker image digest mismatch");
        require(allowedWorkerPolicyHashCount != 0, "worker policy set not configured");
        require(allowedWorkerPolicyHashes[policyHash], "worker policy hash not allowed");
        require(address(tdxV4Attestation) != address(0), "tdx attestation not configured");
    }

    function _requireBoundReportData(bytes memory reportData, bytes32 binding, uint256 nonce) internal pure {
        require(reportData.length == REPORT_DATA_LENGTH, "invalid attestation report data");

        bytes32 quotedBinding;
        bytes32 quotedNonce;
        assembly ("memory-safe") {
            quotedBinding := mload(add(reportData, 0x20))
            quotedNonce := mload(add(reportData, 0x40))
        }
        require(quotedBinding == binding && quotedNonce == bytes32(nonce), "quote report data mismatch");
    }

    function _registerVerifiedDevice(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key
    ) internal {
        addKnownDevice(_address);
        devices[_address] = Device(true, _public_ip, _msg_broker_ip, _public_key);
        registeredRunMemberCount++;
        if (registeredRunMemberCount == committedRunRoster.length) {
            runRosterFrozen = true;
            emit RunRosterFrozen(runRosterDigest, committedRunRoster.length);
        }
    }

    function addKnownDevice(address _address) internal {
        if (!knownDevice[_address]) {
            knownDevice[_address] = true;
            deviceAddresses.push(_address);
        }
    }

    function _deregisterDevice(address _address) internal {
        require(!runRosterFrozen, "run roster is immutable");
        require(actionKeyForParticipant[_address] == msg.sender, "only current action key can deregister");
        require(knownDevice[_address], "device not registered");

        address actionKey = actionKeyForParticipant[_address];
        delete devices[_address];
        delete registeredComposeHashes[_address];
        delete registeredImageDigests[_address];
        delete registeredWorkerPolicyHashes[_address];
        delete selloReceiptKeys[_address];
        delete actionKeyForParticipant[_address];
        delete participantForActionKey[actionKey];
        knownDevice[_address] = false;
        registeredRunMemberCount--;
        _removeDeviceAddress(_address);

        emit DeviceDeregistered(_address, registrationNonces[_address]);
    }

    function _participantForCurrentAction(address actionKey) internal view returns (address participant) {
        participant = participantForActionKey[actionKey];
        require(participant != address(0), "action key not registered");
        require(actionKeyForParticipant[participant] == actionKey, "action key is not current");
    }

    function _removeDeviceAddress(address _address) internal {
        for (uint256 i = 0; i < deviceAddresses.length; i++) {
            if (deviceAddresses[i] != _address) {
                continue;
            }

            uint256 lastIndex = deviceAddresses.length - 1;
            if (i != lastIndex) {
                deviceAddresses[i] = deviceAddresses[lastIndex];
            }
            deviceAddresses.pop();
            return;
        }

        revert("device registry invariant violated");
    }
}
