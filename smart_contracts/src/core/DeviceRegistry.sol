// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AppComposeImage} from "../attestation/AppComposeImage.sol";
import {Rtmr3Event} from "../attestation/Rtmr3Event.sol";

interface ITdxV4Attestation {
    function verifyAndAttestOnChainWithRtmr3EventLog(
        bytes calldata input,
        Rtmr3Event[] calldata rtmr3EventLog,
        bytes32 composeHash
    ) external view returns (bytes memory output);
}

contract DeviceRegistry {
    struct Device {
        bool authorized;
        string public_ip;
        string msg_broker_ip;
        bytes public_key;
    }

    uint256 private constant REPORT_DATA_LENGTH = 64;

    bytes32 public constant REGISTRATION_REPORT_DATA_DOMAIN =
        keccak256("MasterThesis.DeviceRegistry.registration.v4");

    address public owner;
    bytes32 public immutable deploymentId;
    ITdxV4Attestation public tdxV4Attestation;
    bytes32 public expectedWorkerImageDigest;
    mapping(address => Device) public devices;
    mapping(address => uint256) public registrationNonces;
    mapping(address => bytes32) public registeredComposeHashes;
    mapping(address => bytes32) public registeredImageDigests;
    mapping(address => bytes32) public registeredWorkerPolicyHashes;
    mapping(bytes32 => bool) public allowedWorkerPolicyHashes;
    uint256 public allowedWorkerPolicyHashCount;
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

    constructor(bytes32 _deploymentId) {
        require(_deploymentId != bytes32(0), "invalid deployment id");
        owner = msg.sender;
        deploymentId = _deploymentId;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function setTdxV4Attestation(address _tdxV4Attestation) public onlyOwner {
        require(_tdxV4Attestation != address(0), "invalid attestation address");
        tdxV4Attestation = ITdxV4Attestation(_tdxV4Attestation);
    }

    function setExpectedWorkerImageDigest(bytes32 _expectedWorkerImageDigest) public onlyOwner {
        expectedWorkerImageDigest = _expectedWorkerImageDigest;
        emit ExpectedWorkerImageDigestUpdated(_expectedWorkerImageDigest);
    }

    /// @notice Adds or removes one role-specific worker policy derived from selected
    ///         security fields. This is not an allowlist of complete Compose hashes:
    ///         worker-specific environment, endpoints and other runtime values remain
    ///         free to vary and are bound separately through REPORTDATA and RTMR3.
    function setWorkerPolicyHashAllowed(bytes32 policyHash, bool allowed) public onlyOwner {
        require(policyHash != bytes32(0), "invalid worker policy hash");
        bool current = allowedWorkerPolicyHashes[policyHash];
        if (current == allowed) return;

        allowedWorkerPolicyHashes[policyHash] = allowed;
        if (allowed) allowedWorkerPolicyHashCount++;
        else allowedWorkerPolicyHashCount--;
        emit WorkerPolicyHashPermissionUpdated(policyHash, allowed);
    }

    function authorizeAddress(address _address) public onlyOwner {
        require(knownDevice[_address], "device not registered");
        devices[_address].authorized = true;
        emit DeviceAuthorized(_address);
    }

    function deauthorizeAddress(address _address) public onlyOwner {
        require(knownDevice[_address], "device not registered");
        devices[_address].authorized = false;
        emit DeviceDeauthorized(_address);
    }

    function leaveNetwork() public {
        _deregisterDevice(msg.sender);
        emit DeviceLeft(msg.sender);
    }

    /// @notice Removes the caller's own verified registration and workload identity.
    /// @dev The registration nonce is intentionally retained to prevent replay of an old quote.
    function deregisterDevice() external {
        _deregisterDevice(msg.sender);
    }

    function isAuthorized(address _address) public view returns (bool) {
        if (!devices[_address].authorized || expectedWorkerImageDigest == bytes32(0)) {
            return false;
        }
        bytes32 imageDigest = registeredImageDigests[_address];
        bytes32 policyHash = registeredWorkerPolicyHashes[_address];
        return imageDigest == expectedWorkerImageDigest && allowedWorkerPolicyHashes[policyHash];
    }

    function isDeviceRegistrationCurrent(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes memory canonicalAppCompose
    ) external view returns (bool) {
        (bytes32 composeHash, bytes32 imageDigest, bytes32 policyHash) =
            _workloadIdentity(canonicalAppCompose);
        return isAuthorized(_address) && registeredComposeHashes[_address] == composeHash
            && registeredImageDigests[_address] == imageDigest
            && registeredWorkerPolicyHashes[_address] == policyHash
            && keccak256(bytes(devices[_address].public_ip)) == keccak256(bytes(_public_ip))
            && keccak256(bytes(devices[_address].msg_broker_ip)) == keccak256(bytes(_msg_broker_ip))
            && keccak256(devices[_address].public_key) == keccak256(_public_key);
    }

    function getAuthorizedDevices() external view returns (address[] memory) {
        uint256 count = 0;
        for (uint256 i = 0; i < deviceAddresses.length; i++) {
            if (isAuthorized(deviceAddresses[i])) {
                count++;
            }
        }

        address[] memory authorized = new address[](count);
        uint256 index = 0;
        for (uint256 i = 0; i < deviceAddresses.length; i++) {
            address device = deviceAddresses[i];
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
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key
    ) public {
        require(_address == msg.sender, "sender/address mismatch");

        // Image and role policy are derived from the exact app_compose preimage on-chain.
        // The complete preimage is then hashed independently for REPORTDATA and RTMR3 replay.
        (bytes32 composeHash, bytes32 workerImageDigest, bytes32 policyHash) =
            _workloadIdentity(canonicalAppCompose);
        _requireRegistrationPreparation(_address, _public_key, workerImageDigest, policyHash);

        uint256 nonce = registrationNonces[_address];
        bytes32 binding = _registrationBinding(
            _address,
            _public_ip,
            _msg_broker_ip,
            _public_key,
            composeHash,
            workerImageDigest,
            policyHash,
            nonce
        );
        bytes memory output = tdxV4Attestation.verifyAndAttestOnChainWithRtmr3EventLog(
            quote, rtmr3EventLog, composeHash
        );
        _requireBoundReportData(output, binding, nonce);

        registrationNonces[_address] = nonce + 1;
        registeredComposeHashes[_address] = composeHash;
        registeredImageDigests[_address] = workerImageDigest;
        registeredWorkerPolicyHashes[_address] = policyHash;
        _registerVerifiedDevice(_address, _public_ip, _msg_broker_ip, _public_key);
        emit DeviceRegistered(_address, nonce, composeHash);
        emit DeviceWorkerPolicyRegistered(_address, policyHash);
    }

    /// @notice Returns the exact 64 bytes that the TEE must place into TDREPORT.REPORTDATA.
    function registrationReportData(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes memory canonicalAppCompose
    ) public view returns (bytes memory) {
        (bytes32 composeHash, bytes32 workerImageDigest, bytes32 policyHash) =
            _workloadIdentity(canonicalAppCompose);
        _requireRegistrationPreparation(_address, _public_key, workerImageDigest, policyHash);
        uint256 nonce = registrationNonces[_address];
        bytes32 binding = _registrationBinding(
            _address,
            _public_ip,
            _msg_broker_ip,
            _public_key,
            composeHash,
            workerImageDigest,
            policyHash,
            nonce
        );
        return abi.encodePacked(binding, bytes32(nonce));
    }

    function workloadIdentity(bytes memory canonicalAppCompose)
        external
        pure
        returns (bytes32 composeHash, bytes32 workerImageDigest)
    {
        (composeHash, workerImageDigest,) = _workloadIdentity(canonicalAppCompose);
    }

    function workerPolicyHash(bytes memory canonicalAppCompose) external pure returns (bytes32) {
        return AppComposeImage.workerPolicyHash(canonicalAppCompose);
    }

    function _workloadIdentity(bytes memory canonicalAppCompose)
        internal
        pure
        returns (bytes32 composeHash, bytes32 workerImageDigest, bytes32 policyHash)
    {
        (workerImageDigest, policyHash) = AppComposeImage.identity(canonicalAppCompose);
        composeHash = sha256(canonicalAppCompose);
    }

    function _registrationBinding(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 composeHash,
        bytes32 workerImageDigest,
        bytes32 policyHash,
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
                keccak256(bytes(_public_ip)),
                keccak256(bytes(_msg_broker_ip)),
                keccak256(_public_key),
                composeHash,
                workerImageDigest,
                policyHash,
                nonce
            )
        );
    }

    function _requireRegistrationPreparation(
        address _address,
        bytes memory _public_key,
        bytes32 workerImageDigest,
        bytes32 policyHash
    ) internal view {
        require(_address != address(0), "invalid device address");
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
    }

    function addKnownDevice(address _address) internal {
        if (!knownDevice[_address]) {
            knownDevice[_address] = true;
            deviceAddresses.push(_address);
        }
    }

    function _deregisterDevice(address _address) internal {
        require(_address == msg.sender, "only device can deregister itself");
        require(knownDevice[_address], "device not registered");

        delete devices[_address];
        delete registeredComposeHashes[_address];
        delete registeredImageDigests[_address];
        delete registeredWorkerPolicyHashes[_address];
        knownDevice[_address] = false;
        _removeDeviceAddress(_address);

        emit DeviceDeregistered(_address, registrationNonces[_address]);
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
