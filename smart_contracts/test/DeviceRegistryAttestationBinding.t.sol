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
        registry.setWorkerPolicyHashAllowed(registry.workerPolicyHash(appCompose), true);
    }

    function testDerivesImageAndComposeIdentityOnChain() public view {
        (bytes32 composeHash, bytes32 derivedImageDigest) = registry.workloadIdentity(appCompose);
        assertEq(composeHash, sha256(appCompose));
        assertEq(derivedImageDigest, imageDigest);
        assertEq(
            registry.workerPolicyHash(appCompose),
            0xd6f5c4a2c56214addcca217529fc222df7764557d30eb2613445239715401438
        );
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
        assertEq(registry.registeredWorkerPolicyHashes(worker), registry.workerPolicyHash(appCompose));
        assertEq(registry.registrationNonces(worker), 1);
    }

    function testVariableEnvironmentValuesDoNotChangeWorkerPolicy() public view {
        bytes memory changedRuntimeValues = _appCompose(
            "worker-9",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"9\\\"\\n      ACCOUNT_ADDRESS: \\\"0x1234\\\"\\n      PUBLIC_IP: \\\"https://other.example\\\"\\n"
        );
        bytes memory sameKeysDifferentValues = _appCompose(
            "worker-1",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"1\\\"\\n      ACCOUNT_ADDRESS: \\\"0xabcd\\\"\\n      PUBLIC_IP: \\\"https://worker.example\\\"\\n"
        );

        assertTrue(sha256(changedRuntimeValues) != sha256(sameKeysDifferentValues));
        assertEq(
            registry.workerPolicyHash(changedRuntimeValues),
            registry.workerPolicyHash(sameKeysDifferentValues)
        );
    }

    function testUnknownEnvironmentValueChangesPolicyAndIsRejected() public {
        bytes memory dangerous = _appCompose(
            "worker-0",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"1\\\"\\n      NODE_OPTIONS: \\\"--import=/tmp/evil.mjs\\\"\\n"
        );
        assertTrue(registry.workerPolicyHash(dangerous) != registry.workerPolicyHash(appCompose));

        vm.expectRevert(bytes("worker policy hash not allowed"));
        _reportData(registry, dangerous, publicKey);
    }

    function testRejectsDuplicateEnvironmentKey() public {
        bytes memory duplicate = _appCompose(
            "worker-0",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"1\\\"\\n      ROUND: \\\"2\\\"\\n"
        );
        vm.expectRevert(bytes("worker environment key duplicated"));
        registry.workerPolicyHash(duplicate);
    }

    function testRejectsUnknownWorkerServiceField() public {
        bytes memory dangerous = _appCompose(
            "worker-0",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"1\\\"\\n    pid: host\\n"
        );
        vm.expectRevert(bytes("unknown worker service field"));
        registry.workerPolicyHash(dangerous);
    }

    function testRejectsTopLevelVolumeBindOptions() public {
        bytes memory dangerous = _appCompose(
            "worker-0",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"1\\\"\\nvolumes:\\n  participant-key-state:\\n    driver_opts:\\n      type: none\\n      o: \\\"bind\\\"\\n      device: \\\"/tmp/attacker-controlled\\\"\\n"
        );
        vm.expectRevert(bytes("top-level volume options not allowed"));
        registry.workerPolicyHash(dangerous);
    }

    function testOwnerCanProvisionBothWorkerRolePoliciesBeforeRegistration() public {
        bytes memory inferenceRole = _appCompose(
            "worker-0",
            _digestPinnedImage(IMAGE_HEX),
            "ROUND: \\\"1\\\"\\n      TEE_INFERENCE_ENABLED: \\\"1\\\"\\n"
        );
        bytes32 inferencePolicy = registry.workerPolicyHash(inferenceRole);
        assertTrue(inferencePolicy != registry.workerPolicyHash(appCompose));
        registry.setWorkerPolicyHashAllowed(inferencePolicy, true);
        assertEq(registry.allowedWorkerPolicyHashCount(), 2);

        bytes memory reportData = _reportData(registry, inferenceRole, publicKey);
        vm.prank(worker);
        _register(registry, reportData, inferenceRole, publicKey);
        assertTrue(registry.isAuthorized(worker));
        assertEq(registry.registeredWorkerPolicyHashes(worker), inferencePolicy);
    }

    function testPolicyRemovalImmediatelyRevokesRegisteredWorker() public {
        bytes32 policyHash = registry.workerPolicyHash(appCompose);
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);
        assertTrue(registry.isAuthorized(worker));

        registry.setWorkerPolicyHashAllowed(policyHash, false);
        assertFalse(registry.isAuthorized(worker));
        assertEq(registry.allowedWorkerPolicyHashCount(), 0);
    }

    function testWorkerPolicySetIsFailClosed() public {
        DeviceRegistry unconfigured = new DeviceRegistry(keccak256("policy-less deployment"));
        unconfigured.setTdxV4Attestation(address(verifier));
        unconfigured.setExpectedWorkerImageDigest(imageDigest);

        vm.expectRevert(bytes("worker policy set not configured"));
        _reportData(unconfigured, appCompose, publicKey);
    }

    function testCurrentRegistrationRequiresExactBoundEndpoints() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);

        assertTrue(
            registry.isDeviceRegistrationCurrent(
                worker, PUBLIC_IP, BROKER_IP, publicKey, appCompose
            )
        );
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, "https://replacement.example", BROKER_IP, publicKey, appCompose
            )
        );
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, PUBLIC_IP, "tcp://replacement.example:5555", publicKey, appCompose
            )
        );
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

    function testRejectsDuplicateTopLevelServicesField() public {
        bytes memory ambiguous = bytes(
            string.concat(
                '{"docker_compose_file":"services:\\nservices:\\n  dfl-worker:\\n    image: ',
                _digestPinnedImage(IMAGE_HEX),
                '\\n","manifest_version":2,"runner":"docker-compose"}'
            )
        );
        vm.expectRevert(bytes("services field duplicated"));
        registry.workerPolicyHash(ambiguous);
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

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);
    }

    function testImagePolicyIsFailClosed() public {
        DeviceRegistry unconfigured = new DeviceRegistry(keccak256("unconfigured deployment"));
        unconfigured.setTdxV4Attestation(address(verifier));

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

    function testRegisteredDeviceCanDeregisterOnlyItself() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, reportData, appCompose, publicKey);

        vm.expectRevert(bytes("device not registered"));
        vm.prank(attacker);
        registry.deregisterDevice();
        assertTrue(registry.isAuthorized(worker));

        vm.prank(worker);
        registry.deregisterDevice();

        assertFalse(registry.isAuthorized(worker));
        assertEq(registry.registeredComposeHashes(worker), bytes32(0));
        assertEq(registry.registeredImageDigests(worker), bytes32(0));
        assertEq(registry.registeredWorkerPolicyHashes(worker), bytes32(0));
        assertEq(registry.registrationNonces(worker), 1);

        (bool authorized, string memory publicIp, string memory brokerIp, bytes memory storedKey) =
            registry.getDevice(worker);
        assertFalse(authorized);
        assertEq(bytes(publicIp).length, 0);
        assertEq(bytes(brokerIp).length, 0);
        assertEq(storedKey.length, 0);
        assertEq(registry.getAuthorizedDevices().length, 0);
    }

    function testDeregisteredDeviceCanRegisterAgainWithoutQuoteReplay() public {
        bytes memory firstReportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, firstReportData, appCompose, publicKey);

        vm.prank(worker);
        registry.deregisterDevice();

        vm.expectRevert(bytes("quote report data mismatch"));
        vm.prank(worker);
        _register(registry, firstReportData, appCompose, publicKey);

        bytes memory freshReportData = _reportData(registry, appCompose, publicKey);
        vm.prank(worker);
        _register(registry, freshReportData, appCompose, publicKey);

        assertTrue(registry.isAuthorized(worker));
        assertEq(registry.registrationNonces(worker), 2);
        address[] memory authorizedDevices = registry.getAuthorizedDevices();
        assertEq(authorizedDevices.length, 1);
        assertEq(authorizedDevices[0], worker);
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
