// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ITdxV4Attestation {
    function verifyAndAttestOnChain(bytes calldata input) external view returns (bytes memory output);
    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata input, bytes[] calldata rtmr3EventDigests)
        external
        view
        returns (bytes memory output);
}

interface IRtmr3ReplayPolicy {
    function verifyReplayFromQuote(bytes calldata quote, bytes[] calldata rtmr3EventDigests) external view;
}

contract DeviceRegistry {
    struct Device {
        bool authorized;
        string public_ip;
        string msg_broker_ip;
        bytes public_key;
    }

    address public owner;
    ITdxV4Attestation public tdxV4Attestation;
    IRtmr3ReplayPolicy public rtmr3ReplayPolicy;
    mapping(address => Device) public devices;
    mapping(address => bool) private knownDevice;
    address[] private deviceAddresses;
    uint256 public number;

    event DeviceRegistered(address indexed device);
    event DeviceAuthorized(address indexed device);
    event DeviceDeauthorized(address indexed device);
    event DeviceLeft(address indexed device);

    constructor() {
        owner = msg.sender;
        // Add some initial authorized addresses for demonstration
        devices[msg.sender] = Device(true, "test_ip", "", "");
        addKnownDevice(msg.sender);
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function setTdxV4Attestation(address _tdxV4Attestation) public onlyOwner {
        require(_tdxV4Attestation != address(0), "invalid attestation address");
        tdxV4Attestation = ITdxV4Attestation(_tdxV4Attestation);
    }

    function setRtmr3ReplayPolicy(address _rtmr3ReplayPolicy) public onlyOwner {
        rtmr3ReplayPolicy = IRtmr3ReplayPolicy(_rtmr3ReplayPolicy);
    }

    function authorizeAddress(address _address) public onlyOwner {
        addKnownDevice(_address);
        devices[_address].authorized = true;
        emit DeviceAuthorized(_address);
    }

    function deauthorizeAddress(address _address) public onlyOwner {
        devices[_address].authorized = false;
        emit DeviceDeauthorized(_address);
    }

    function leaveNetwork() public {
        require(knownDevice[msg.sender], "device not registered");
        require(devices[msg.sender].authorized, "device not authorized");
        devices[msg.sender].authorized = false;
        emit DeviceLeft(msg.sender);
    }

    function isAuthorized(address _address) public view returns (bool) {
        if (devices[_address].authorized) {
            return true;
        } else {
            return false;
        }
    }

    function getAuthorizedDevices() external view returns (address[] memory) {
        uint256 count = 0;
        for (uint256 i = 0; i < deviceAddresses.length; i++) {
            if (devices[deviceAddresses[i]].authorized) {
                count++;
            }
        }

        address[] memory authorized = new address[](count);
        uint256 index = 0;
        for (uint256 i = 0; i < deviceAddresses.length; i++) {
            address device = deviceAddresses[i];
            if (devices[device].authorized) {
                authorized[index] = device;
                index++;
            }
        }
        return authorized;
    }

    function getDevice(
        address _address
    ) external view returns (bool, string memory, string memory, bytes memory) {
        return (
            devices[_address].authorized,
            devices[_address].public_ip,
            devices[_address].msg_broker_ip,
            devices[_address].public_key
        );
    }

    function registerDevice(
        bytes calldata quote,
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key
    ) public {
        require(address(tdxV4Attestation) != address(0), "tdx attestation not configured");
        tdxV4Attestation.verifyAndAttestOnChain(quote);
        _registerVerifiedDevice(_address, _public_ip, _msg_broker_ip, _public_key);
    }

    function registerDeviceWithRtmr3Events(
        bytes calldata quote,
        bytes[] calldata rtmr3EventDigests,
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key
    ) public {
        require(address(tdxV4Attestation) != address(0), "tdx attestation not configured");
        if (address(rtmr3ReplayPolicy) != address(0)) {
            tdxV4Attestation.verifyAndAttestOnChain(quote);
            rtmr3ReplayPolicy.verifyReplayFromQuote(quote, rtmr3EventDigests);
        } else {
            tdxV4Attestation.verifyAndAttestOnChainWithRtmr3Events(quote, rtmr3EventDigests);
        }
        _registerVerifiedDevice(_address, _public_ip, _msg_broker_ip, _public_key);
    }

    function _registerVerifiedDevice(
        address _address,
        string memory _public_ip,
        string memory _msg_broker_ip,
        bytes memory _public_key
    ) internal {
        addKnownDevice(_address);
        devices[_address] = Device(
            true,
            _public_ip,
            _msg_broker_ip,
            _public_key
        );
        emit DeviceRegistered(_address);
    }

    function addKnownDevice(address _address) internal {
        if (!knownDevice[_address]) {
            knownDevice[_address] = true;
            deviceAddresses.push(_address);
        }
    }
}
