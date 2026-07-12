// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {MockTdxV4Attestation} from "../src/attestation/MockTdxV4Attestation.sol";

contract MalformedTdxV4Attestation {
    bytes private verifierOutput;

    function setOutput(bytes calldata output) external {
        verifierOutput = output;
    }

    function verifyAndAttestOnChain(bytes calldata) external view returns (bytes memory) {
        return verifierOutput;
    }

    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata, bytes[] calldata)
        external
        view
        returns (bytes memory)
    {
        return verifierOutput;
    }

    function verifyAndAttestOnChainWithRtmr3Events(bytes calldata, bytes[] calldata, bytes32)
        external
        view
        returns (bytes memory)
    {
        return verifierOutput;
    }
}

contract DeviceRegistryAttestationBindingTest is Test {
    DeviceRegistry private registry;
    MockTdxV4Attestation private verifier;

    address private worker;
    address private attacker;
    bytes private publicKey;
    bytes32 private composeHash;
    bytes32 private imageDigest;
    string private constant PUBLIC_IP = "https://worker.example";
    string private constant BROKER_IP = "tcp://worker.example:5555";

    function setUp() public {
        worker = makeAddr("worker");
        attacker = makeAddr("attacker");
        publicKey = hex"30820122300d06092a864886f70d010101050003";
        composeHash = keccak256("approved worker compose");
        imageDigest = sha256("approved worker image");

        registry = new DeviceRegistry(keccak256("deployment one"));
        verifier = new MockTdxV4Attestation();
        registry.setTdxV4Attestation(address(verifier));
        registry.setExpectedWorkerImageDigest(imageDigest);
        registry.setWorkerComposePolicy(composeHash, imageDigest, true);
        registry.setRegistrationAllowed(worker, true);
    }

    function testRegistersOnlyWithBoundReportData() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);
        assertEq(reportData.length, 64);

        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);

        (bool authorized, string memory publicIp, string memory brokerIp, bytes memory storedKey) =
            registry.getDevice(worker);
        assertTrue(authorized);
        assertEq(publicIp, PUBLIC_IP);
        assertEq(brokerIp, BROKER_IP);
        assertEq(storedKey, publicKey);
        assertEq(registry.registrationNonces(worker), 1);
        assertFalse(registry.registrationAllowed(worker));
    }

    function testRejectsThirdPartyRegistrationForWorker() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);

        vm.expectRevert(bytes("sender/address mismatch"));
        vm.prank(attacker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);
    }

    function testRejectsQuoteReplayAfterNonceIncrement() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);

        vm.startPrank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);
        vm.stopPrank();

        registry.setRegistrationAllowed(worker, true);
        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);
    }

    function testRejectsExpiredRegistrationChallenge() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);
        vm.warp(registry.registrationChallengeDeadlines(worker) + 1);

        vm.expectRevert(bytes("registration challenge expired"));
        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);
    }

    function testRejectsChangedPublicKeyOrEndpoint() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, hex"010203", composeHash, imageDigest);

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        registry.registerDeviceWithRtmr3EventsAndImageDigest(
            reportData,
            _events(),
            composeHash,
            imageDigest,
            worker,
            "https://modified.example",
            BROKER_IP,
            publicKey
        );
    }

    function testRejectsUnapprovedComposeEvenWithExpectedImageClaim() public {
        bytes32 unapprovedCompose = keccak256("unapproved compose");
        bytes memory reportData = _reportData(registry, publicKey, unapprovedCompose, imageDigest);

        vm.expectRevert(bytes("worker compose not allowed"));
        vm.prank(worker);
        _register(registry, reportData, publicKey, unapprovedCompose, imageDigest);
    }

    function testRejectsWrongComposeImagePair() public {
        bytes32 otherImage = sha256("different image");
        registry.setExpectedWorkerImageDigest(otherImage);
        bytes memory reportData = _reportData(registry, publicKey, composeHash, otherImage);

        vm.expectRevert(bytes("compose/image policy mismatch"));
        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, otherImage);
    }

    function testImagePolicyIsFailClosed() public {
        DeviceRegistry unconfigured = new DeviceRegistry(keccak256("unconfigured deployment"));
        unconfigured.setTdxV4Attestation(address(verifier));
        unconfigured.setRegistrationAllowed(worker, true);
        unconfigured.setWorkerComposePolicy(composeHash, imageDigest, true);
        bytes memory reportData = _reportData(unconfigured, publicKey, composeHash, imageDigest);

        vm.expectRevert(bytes("worker image policy not configured"));
        vm.prank(worker);
        _register(unconfigured, reportData, publicKey, composeHash, imageDigest);
    }

    function testRejectsMalformedVerifierOutput() public {
        MalformedTdxV4Attestation malformed = new MalformedTdxV4Attestation();
        malformed.setOutput(hex"00");
        registry.setTdxV4Attestation(address(malformed));

        vm.expectRevert(bytes("invalid attestation output"));
        vm.prank(worker);
        _register(registry, hex"00", publicKey, composeHash, imageDigest);
        assertEq(registry.registrationNonces(worker), 0);

        malformed.setOutput(new bytes(120));
        vm.expectRevert(bytes("invalid attestation output"));
        vm.prank(worker);
        _register(registry, hex"00", publicKey, composeHash, imageDigest);
        assertEq(registry.registrationNonces(worker), 0);
    }

    function testVerifierRevertDoesNotConsumeChallengeOrNonce() public {
        bytes32 challenge = registry.registrationChallenges(worker);

        vm.expectRevert(bytes("mock report data must be 64 bytes"));
        vm.prank(worker);
        _register(registry, hex"00", publicKey, composeHash, imageDigest);

        assertEq(registry.registrationNonces(worker), 0);
        assertEq(registry.registrationChallenges(worker), challenge);
        assertTrue(registry.registrationAllowed(worker));
    }

    function testQuoteCannotCrossRegistryDeployments() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);
        DeviceRegistry other = new DeviceRegistry(keccak256("deployment two"));
        other.setTdxV4Attestation(address(verifier));
        other.setExpectedWorkerImageDigest(imageDigest);
        other.setWorkerComposePolicy(composeHash, imageDigest, true);
        other.setRegistrationAllowed(worker, true);

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(other, reportData, publicKey, composeHash, imageDigest);
    }

    function testQuoteCannotCrossChainIds() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);
        vm.chainId(block.chainid + 1);

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);
    }

    function testPolicyRotationRevokesExistingRegistration() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);
        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);
        assertTrue(registry.isAuthorized(worker));

        registry.setWorkerComposePolicy(composeHash, imageDigest, false);
        assertFalse(registry.isAuthorized(worker));
        assertEq(registry.getAuthorizedDevices().length, 0);
    }

    function testImageRotationRevokesExistingRegistration() public {
        bytes memory reportData = _reportData(registry, publicKey, composeHash, imageDigest);
        vm.prank(worker);
        _register(registry, reportData, publicKey, composeHash, imageDigest);

        registry.setExpectedWorkerImageDigest(sha256("next worker image"));
        assertFalse(registry.isAuthorized(worker));
    }

    function testLegacySelectorsCannotBypassBinding() public {
        vm.expectRevert(bytes("bound registration required"));
        registry.registerDevice(hex"00", worker, PUBLIC_IP, BROKER_IP, publicKey);

        vm.expectRevert(bytes("bound registration required"));
        registry.registerDeviceWithRtmr3Events(
            hex"00", _events(), composeHash, worker, PUBLIC_IP, BROKER_IP, publicKey
        );
    }

    function testOwnerCannotAuthorizeUnknownAddress() public {
        vm.expectRevert(bytes("device not registered"));
        registry.authorizeAddress(worker);
    }

    function _reportData(
        DeviceRegistry target,
        bytes memory key,
        bytes32 targetComposeHash,
        bytes32 targetImageDigest
    ) private view returns (bytes memory) {
        return target.registrationReportData(
            worker, PUBLIC_IP, BROKER_IP, key, targetComposeHash, targetImageDigest
        );
    }

    function _register(
        DeviceRegistry target,
        bytes memory quote,
        bytes memory key,
        bytes32 targetComposeHash,
        bytes32 targetImageDigest
    ) private {
        target.registerDeviceWithRtmr3EventsAndImageDigest(
            quote,
            _events(),
            targetComposeHash,
            targetImageDigest,
            worker,
            PUBLIC_IP,
            BROKER_IP,
            key
        );
    }

    function _events() private pure returns (bytes[] memory events) {
        events = new bytes[](1);
        events[0] = new bytes(48);
    }
}
