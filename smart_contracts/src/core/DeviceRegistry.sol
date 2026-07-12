// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ITdxV4Attestation {
    function verifyAndAttestOnChain(bytes calldata input) external view returns (bytes memory output);
    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata input, bytes[] calldata rtmr3EventDigests)
        external
        view
        returns (bytes memory output);
    function verifyAndAttestOnChainWithRtmr3Events(
        bytes calldata input,
        bytes[] calldata rtmr3EventDigests,
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

    struct WorkerComposePolicy {
        bytes32 imageDigest;
        bool allowed;
    }

    uint256 private constant ATTESTATION_OUTPUT_LENGTH = 119;

    bytes32 public constant REGISTRATION_REPORT_DATA_DOMAIN =
        keccak256("MasterThesis.DeviceRegistry.registration.v1");
    bytes32 public constant REGISTRATION_CHALLENGE_DOMAIN =
        keccak256("MasterThesis.DeviceRegistry.challenge.v1");
    uint256 public constant REGISTRATION_CHALLENGE_TTL = 1 days;

    address public owner;
    bytes32 public immutable deploymentId;
    ITdxV4Attestation public tdxV4Attestation;
    bytes32 public expectedWorkerImageDigest;
    mapping(address => Device) public devices;
    mapping(address => bool) public registrationAllowed;
    mapping(address => bytes32) public registrationChallenges;
    mapping(address => uint256) public registrationChallengeDeadlines;
    mapping(address => uint256) public registrationNonces;
    mapping(address => bytes32) public registeredComposeHashes;
    mapping(address => bytes32) public registeredImageDigests;
    mapping(bytes32 => WorkerComposePolicy) public workerComposePolicies;
    mapping(address => bool) private knownDevice;
    address[] private deviceAddresses;
    uint256 public number;

    event DeviceRegistered(address indexed device, uint256 indexed registrationNonce, bytes32 indexed composeHash);
    event DeviceAuthorized(address indexed device);
    event DeviceDeauthorized(address indexed device);
    event DeviceLeft(address indexed device);
    event RegistrationPermissionUpdated(
        address indexed device, bool allowed, bytes32 indexed challenge, uint256 deadline
    );
    event WorkerComposePolicyUpdated(bytes32 indexed composeHash, bytes32 indexed imageDigest, bool allowed);
    event ExpectedWorkerImageDigestUpdated(bytes32 expectedWorkerImageDigest);

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

    function setRegistrationAllowed(address _address, bool allowed) external onlyOwner {
        require(_address != address(0), "invalid device address");
        registrationAllowed[_address] = allowed;
        if (allowed) {
            bytes32 previousBlockHash = block.number == 0 ? bytes32(0) : blockhash(block.number - 1);
            bytes32 challenge = keccak256(
                abi.encode(
                    REGISTRATION_CHALLENGE_DOMAIN,
                    deploymentId,
                    _address,
                    registrationNonces[_address],
                    previousBlockHash,
                    block.prevrandao,
                    block.timestamp
                )
            );
            uint256 deadline = block.timestamp + REGISTRATION_CHALLENGE_TTL;
            registrationChallenges[_address] = challenge;
            registrationChallengeDeadlines[_address] = deadline;
            emit RegistrationPermissionUpdated(_address, true, challenge, deadline);
        } else {
            _clearRegistrationPermission(_address);
        }
    }

    function setWorkerComposePolicy(bytes32 composeHash, bytes32 imageDigest, bool allowed) external onlyOwner {
        require(composeHash != bytes32(0), "invalid compose hash");
        if (allowed) {
            require(imageDigest != bytes32(0), "invalid image digest");
        }
        workerComposePolicies[composeHash] = WorkerComposePolicy(imageDigest, allowed);
        emit WorkerComposePolicyUpdated(composeHash, imageDigest, allowed);
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
        require(knownDevice[msg.sender], "device not registered");
        require(isAuthorized(msg.sender), "device not authorized");
        devices[msg.sender].authorized = false;
        emit DeviceLeft(msg.sender);
    }

    function isAuthorized(address _address) public view returns (bool) {
        if (!devices[_address].authorized || expectedWorkerImageDigest == bytes32(0)) {
            return false;
        }
        bytes32 composeHash = registeredComposeHashes[_address];
        bytes32 imageDigest = registeredImageDigests[_address];
        WorkerComposePolicy memory composePolicy = workerComposePolicies[composeHash];
        return imageDigest == expectedWorkerImageDigest && composePolicy.allowed
            && composePolicy.imageDigest == imageDigest;
    }

    function isDeviceRegistrationCurrent(
        address _address,
        bytes memory _public_key,
        bytes32 composeHash,
        bytes32 imageDigest
    ) external view returns (bool) {
        return isAuthorized(_address) && registeredComposeHashes[_address] == composeHash
            && registeredImageDigests[_address] == imageDigest
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
        bytes calldata quote,
        bytes[] calldata rtmr3EventDigests,
        bytes32 composeHash,
        bytes32 workerImageDigest,
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key
    ) public {
        require(_address == msg.sender, "sender/address mismatch");
        require(registrationAllowed[_address], "registration not allowed");
        require(registrationChallenges[_address] != bytes32(0), "registration challenge missing");
        require(block.timestamp <= registrationChallengeDeadlines[_address], "registration challenge expired");
        require(_public_key.length != 0, "public key required");
        require(expectedWorkerImageDigest != bytes32(0), "worker image policy not configured");
        require(workerImageDigest == expectedWorkerImageDigest, "worker image digest mismatch");

        WorkerComposePolicy memory composePolicy = workerComposePolicies[composeHash];
        require(composePolicy.allowed, "worker compose not allowed");
        require(composePolicy.imageDigest == workerImageDigest, "compose/image policy mismatch");
        require(address(tdxV4Attestation) != address(0), "tdx attestation not configured");

        uint256 nonce = registrationNonces[_address];
        bytes32 binding = _registrationBinding(
            _address,
            _public_ip,
            _msg_broker_ip,
            _public_key,
            composeHash,
            workerImageDigest,
            nonce
        );
        bytes memory output = tdxV4Attestation.verifyAndAttestOnChainWithRtmr3Events(
            quote, rtmr3EventDigests, composeHash
        );
        _requireBoundReportData(output, binding, nonce);

        registrationNonces[_address] = nonce + 1;
        _clearRegistrationPermission(_address);
        registeredComposeHashes[_address] = composeHash;
        registeredImageDigests[_address] = workerImageDigest;
        _registerVerifiedDevice(_address, _public_ip, _msg_broker_ip, _public_key);
        emit DeviceRegistered(_address, nonce, composeHash);
    }

    /// @notice Returns the exact 64 bytes that the TEE must place into TDREPORT.REPORTDATA.
    function registrationReportData(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 composeHash,
        bytes32 workerImageDigest
    ) public view returns (bytes memory) {
        uint256 nonce = registrationNonces[_address];
        bytes32 binding = _registrationBinding(
            _address,
            _public_ip,
            _msg_broker_ip,
            _public_key,
            composeHash,
            workerImageDigest,
            nonce
        );
        return abi.encodePacked(binding, bytes32(nonce));
    }

    function _registrationBinding(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key,
        bytes32 composeHash,
        bytes32 workerImageDigest,
        uint256 nonce
    ) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                REGISTRATION_REPORT_DATA_DOMAIN,
                deploymentId,
                block.chainid,
                address(this),
                _address,
                keccak256(bytes(_public_ip)),
                keccak256(bytes(_msg_broker_ip)),
                keccak256(_public_key),
                composeHash,
                workerImageDigest,
                registrationChallenges[_address],
                registrationChallengeDeadlines[_address],
                nonce
            )
        );
    }

    function _requireBoundReportData(bytes memory output, bytes32 binding, uint256 nonce) internal pure {
        require(output.length == ATTESTATION_OUTPUT_LENGTH, "invalid attestation output");

        bytes32 quotedBinding;
        bytes32 quotedNonce;
        assembly ("memory-safe") {
            // bytes data starts at output + 0x20; REPORTDATA starts 49 bytes into the verifier output.
            quotedBinding := mload(add(output, 0x51))
            quotedNonce := mload(add(output, 0x71))
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

    function _clearRegistrationPermission(address _address) internal {
        registrationAllowed[_address] = false;
        delete registrationChallenges[_address];
        delete registrationChallengeDeadlines[_address];
        emit RegistrationPermissionUpdated(_address, false, bytes32(0), 0);
    }

    function addKnownDevice(address _address) internal {
        if (!knownDevice[_address]) {
            knownDevice[_address] = true;
            deviceAddresses.push(_address);
        }
    }
}
