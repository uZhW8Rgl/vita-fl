// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {MockTdxV4Attestation} from "../src/attestation/MockTdxV4Attestation.sol";
import {Rtmr3Event} from "../src/attestation/Rtmr3Event.sol";

contract MalformedTdxV4Attestation {
    bytes private verifierOutput;

    function setOutput(bytes calldata output) external {
        verifierOutput = output;
    }

    function verifyAndAttestOnChainWithRtmr3EventLog(bytes calldata, Rtmr3Event[] calldata, bytes32)
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
    bytes32 private imageDigest;
    bytes private appCompose;

    string private constant IMAGE_REPOSITORY = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker";
    string private constant IMAGE_HEX = "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";
    string private constant PUBLIC_IP = "https://worker.example";
    string private constant BROKER_IP = "tcp://worker.example:5555";

    function setUp() public {
        worker = makeAddr("worker");
        attacker = makeAddr("attacker");
        publicKey = hex"30820122300d06092a864886f70d010101050003";
        imageDigest = 0x4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553;
        appCompose = _appCompose("worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"1\\\"\\n");

        registry = new DeviceRegistry(keccak256("deployment one"));
        verifier = new MockTdxV4Attestation();
        registry.setTdxV4Attestation(address(verifier));
        registry.setExpectedWorkerImageDigest(imageDigest);
        registry.setRegistrationAllowed(worker, true);
    }

    function testDerivesImageAndComposeIdentityOnChain() public view {
        (bytes32 composeHash, bytes32 derivedImageDigest) = registry.workloadIdentity(appCompose);
        assertEq(composeHash, sha256(appCompose));
        assertEq(derivedImageDigest, imageDigest);
    }

    function testRegistersOnlyWithBoundAppComposeEvidence() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        assertEq(reportData.length, 64);

        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);

        (bool authorized, string memory publicIp, string memory brokerIp, bytes memory storedKey) =
            registry.getDevice(worker);
        assertTrue(authorized);
        assertEq(publicIp, PUBLIC_IP);
        assertEq(brokerIp, BROKER_IP);
        assertEq(storedKey, publicKey);
        assertEq(registry.registeredComposeHashes(worker), sha256(appCompose));
        assertEq(registry.registeredImageDigests(worker), imageDigest);
        assertEq(registry.registrationNonces(worker), 1);
    }

    function testDifferentComposeHashesWithSameImageAreAccepted() public {
        bytes memory changedCompose = _appCompose("worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"9\\\"\\n");
        assertTrue(sha256(changedCompose) != sha256(appCompose));

        bytes memory reportData = _reportData(registry, changedCompose, publicKey);
        vm.prank(worker);
        _register(registry, reportData, changedCompose, publicKey);

        assertTrue(registry.isAuthorized(worker));
        assertEq(registry.registeredComposeHashes(worker), sha256(changedCompose));
        assertEq(registry.registeredImageDigests(worker), imageDigest);
    }

    function testRejectsWrongImageDigestDerivedFromAppCompose() public {
        bytes memory wrongCompose = _appCompose(
            "worker-0",
            _digestPinnedImage("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ""
        );
        vm.expectRevert(bytes("worker image digest mismatch"));
        _reportData(registry, wrongCompose, publicKey);

        vm.expectRevert(bytes("worker image digest mismatch"));
        vm.prank(worker);
        _register(registry, new bytes(64), wrongCompose, publicKey);
    }

    function testRejectsTagOnlyImage() public {
        bytes memory tagOnly = _appCompose("worker-0", string.concat(IMAGE_REPOSITORY, ":phala"), "");
        vm.expectRevert(bytes("worker image must be digest-pinned"));
        registry.workloadIdentity(tagOnly);
    }

    function testDigestInCommentDoesNotAuthorizeWrongImage() public {
        bytes memory smuggled = _appCompose(
            "worker-0",
            _digestPinnedImage("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            string.concat("# approved ", _digestPinnedImage(IMAGE_HEX), "\\n")
        );
        vm.expectRevert(bytes("worker image digest mismatch"));
        _reportData(registry, smuggled, publicKey);

        vm.expectRevert(bytes("worker image digest mismatch"));
        vm.prank(worker);
        _register(registry, new bytes(64), smuggled, publicKey);
    }

    function testRejectsDummyOrSidecarService() public {
        bytes memory ambiguous = bytes(
            string.concat(
                '{"docker_compose_file":"services:\\n  dfl-worker:\\n    image: ',
                _digestPinnedImage(IMAGE_HEX),
                '\\n  sidecar:\\n    image: evil.example/sidecar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n","manifest_version":2,"runner":"docker-compose"}'
            )
        );
        vm.expectRevert(bytes("worker service missing or ambiguous"));
        registry.workloadIdentity(ambiguous);
    }

    function testRejectsFlowStyleSidecarService() public {
        bytes memory ambiguous = bytes(
            string.concat(
                '{"docker_compose_file":"services:\\n  dfl-worker:\\n    image: ',
                _digestPinnedImage(IMAGE_HEX),
                '\\n  sidecar: {image: evil.example/sidecar@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}\\n","manifest_version":2,"runner":"docker-compose"}'
            )
        );
        vm.expectRevert(bytes("worker service missing or ambiguous"));
        registry.workloadIdentity(ambiguous);
    }

    function testChangedAppComposeDoesNotMatchQuotedReportData() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        bytes memory changedCompose = _appCompose("worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"2\\\"\\n");

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, changedCompose, publicKey);
    }

    function testRejectsThirdPartyRegistrationForWorker() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.expectRevert(bytes("sender/address mismatch"));
        vm.prank(attacker);
        _register(registry, reportData, appCompose, publicKey);
    }

    function testRejectsQuoteReplayAfterNonceIncrement() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);

        registry.setRegistrationAllowed(worker, true);
        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);
    }

    function testImagePolicyIsFailClosed() public {
        DeviceRegistry unconfigured = new DeviceRegistry(keccak256("unconfigured deployment"));
        unconfigured.setTdxV4Attestation(address(verifier));
        unconfigured.setRegistrationAllowed(worker, true);

        vm.expectRevert(bytes("worker image policy not configured"));
        _reportData(unconfigured, appCompose, publicKey);

        vm.expectRevert(bytes("worker image policy not configured"));
        vm.prank(worker);
        _register(unconfigured, new bytes(64), appCompose, publicKey);
    }

    function testImageRotationRevokesExistingRegistration() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);
        assertTrue(registry.isAuthorized(worker));

        registry.setExpectedWorkerImageDigest(sha256("next worker image"));
        assertFalse(registry.isAuthorized(worker));
    }

    function testRejectsMalformedVerifierOutput() public {
        MalformedTdxV4Attestation malformed = new MalformedTdxV4Attestation();
        malformed.setOutput(hex"00");
        registry.setTdxV4Attestation(address(malformed));

        vm.expectRevert(bytes("invalid attestation report data"));
        vm.prank(worker);
        _register(registry, hex"00", appCompose, publicKey);
    }

    function testRejectsLegacyPackedVerifierOutput() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        MalformedTdxV4Attestation malformed = new MalformedTdxV4Attestation();
        malformed.setOutput(abi.encodePacked(bytes1(0), new bytes(48), reportData, bytes6(0)));
        registry.setTdxV4Attestation(address(malformed));

        vm.expectRevert(bytes("invalid attestation report data"));
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);
    }

    function testVerifierRotationInvalidatesPreparedReportData() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        registry.setTdxV4Attestation(address(new MockTdxV4Attestation()));

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);
    }

    function testReportDataPreparationRequiresRegistrationPermission() public {
        DeviceRegistry target = new DeviceRegistry(keccak256("permission test"));
        target.setTdxV4Attestation(address(verifier));
        target.setExpectedWorkerImageDigest(imageDigest);

        vm.expectRevert(bytes("registration not allowed"));
        _reportData(target, appCompose, publicKey);
    }

    function testReportDataPreparationRejectsExpiredChallenge() public {
        vm.warp(registry.registrationChallengeDeadlines(worker) + 1);

        vm.expectRevert(bytes("registration challenge expired"));
        _reportData(registry, appCompose, publicKey);
    }

    function testReportDataPreparationRequiresPublicKey() public {
        vm.expectRevert(bytes("public key required"));
        _reportData(registry, appCompose, new bytes(0));
    }

    function testLegacySelectorCannotBypassAppComposeEvidence() public {
        vm.expectRevert(bytes("app compose evidence required"));
        registry.registerDeviceWithRtmr3EventsAndImageDigest(
            hex"00", new bytes[](0), bytes32(0), imageDigest, worker, PUBLIC_IP, BROKER_IP, publicKey
        );
    }

    function _reportData(DeviceRegistry target, bytes memory compose, bytes memory key)
        private
        view
        returns (bytes memory)
    {
        return target.registrationReportData(worker, PUBLIC_IP, BROKER_IP, key, compose);
    }

    function _register(DeviceRegistry target, bytes memory quote, bytes memory compose, bytes memory key) private {
        target.registerDeviceWithAttestedAppCompose(
            quote, _events(), compose, worker, PUBLIC_IP, BROKER_IP, key
        );
    }

    function _events() private pure returns (Rtmr3Event[] memory events) {
        events = new Rtmr3Event[](1);
        events[0] = Rtmr3Event(0x08000001, "compose-hash", new bytes(32));
    }

    function _digestPinnedImage(string memory digestHex) private pure returns (string memory) {
        return string.concat(IMAGE_REPOSITORY, "@sha256:", digestHex);
    }

    function _appCompose(string memory name, string memory image, string memory extraLine)
        private
        pure
        returns (bytes memory)
    {
        return bytes(
            string.concat(
                '{"docker_compose_file":"services:\\n  dfl-worker:\\n    image: ',
                image,
                "\\n    environment:\\n      ",
                extraLine,
                '","manifest_version":2,"name":"',
                name,
                '","runner":"docker-compose"}'
            )
        );
    }
}
