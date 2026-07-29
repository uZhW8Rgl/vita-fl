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
    uint256 private workerPrivateKey;
    address private actionKey;
    address private attacker;
    uint256 private attackerPrivateKey;
    bytes private publicKey;
    bytes32 private imageDigest;
    bytes private appCompose;

    string private constant IMAGE_REPOSITORY = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker";
    string private constant IMAGE_HEX = "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";
    string private constant PUBLIC_IP = "https://worker.example";
    string private constant BROKER_IP = "tcp://worker.example:5555";

    function setUp() public {
        (worker, workerPrivateKey) = makeAddrAndKey("worker");
        actionKey = makeAddr("worker-action-key");
        (attacker, attackerPrivateKey) = makeAddrAndKey("attacker");
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
            registry.workerPolicyHash(appCompose), 0xafdf5af53b2261b7033b16222437b53335c6fa5ae3e71303e90336f272c3cf36
        );
    }

    function testRegistersOnlyWithBoundAppComposeEvidence() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        assertEq(reportData.length, 64);

        vm.prank(actionKey);
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
        assertEq(registry.actionKeyForParticipant(worker), actionKey);
        assertEq(registry.participantForActionKey(actionKey), worker);
        assertEq(registry.resolveAuthorizedParticipant(actionKey), worker);
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
        assertEq(registry.workerPolicyHash(changedRuntimeValues), registry.workerPolicyHash(sameKeysDifferentValues));
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
        bytes memory duplicate =
            _appCompose("worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"1\\\"\\n      ROUND: \\\"2\\\"\\n");
        vm.expectRevert(bytes("worker environment key duplicated"));
        registry.workerPolicyHash(duplicate);
    }

    function testRejectsUnknownWorkerServiceField() public {
        bytes memory dangerous =
            _appCompose("worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"1\\\"\\n    pid: host\\n");
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
            "worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"1\\\"\\n      TEE_INFERENCE_ENABLED: \\\"1\\\"\\n"
        );
        bytes32 inferencePolicy = registry.workerPolicyHash(inferenceRole);
        assertTrue(inferencePolicy != registry.workerPolicyHash(appCompose));
        registry.setWorkerPolicyHashAllowed(inferencePolicy, true);
        assertEq(registry.allowedWorkerPolicyHashCount(), 2);

        bytes memory reportData = _reportData(registry, inferenceRole, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, inferenceRole, publicKey);
        assertTrue(registry.isAuthorized(worker));
        assertEq(registry.registeredWorkerPolicyHashes(worker), inferencePolicy);
    }

    function testPolicyRemovalImmediatelyRevokesRegisteredWorker() public {
        bytes32 policyHash = registry.workerPolicyHash(appCompose);
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);
        assertTrue(registry.isAuthorized(worker));

        registry.setWorkerPolicyHashAllowed(policyHash, false);
        assertFalse(registry.isAuthorized(worker));
        assertEq(registry.allowedWorkerPolicyHashCount(), 0);

        vm.expectRevert(bytes("device already registered"));
        _reportData(registry, appCompose, publicKey);
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
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);

        assertTrue(registry.isDeviceRegistrationCurrent(worker, actionKey, PUBLIC_IP, BROKER_IP, publicKey, appCompose));
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, actionKey, "https://replacement.example", BROKER_IP, publicKey, appCompose
            )
        );
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, actionKey, PUBLIC_IP, "tcp://replacement.example:5555", publicKey, appCompose
            )
        );
    }

    function testDifferentComposeHashesWithSameImageAreAccepted() public {
        bytes memory changedCompose = _appCompose("worker-0", _digestPinnedImage(IMAGE_HEX), "ROUND: \\\"9\\\"\\n");
        assertTrue(sha256(changedCompose) != sha256(appCompose));

        bytes memory reportData = _reportData(registry, changedCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, changedCompose, publicKey);

        assertTrue(registry.isAuthorized(worker));
        assertEq(registry.registeredComposeHashes(worker), sha256(changedCompose));
        assertEq(registry.registeredImageDigests(worker), imageDigest);
    }

    function testRejectsWrongImageDigestDerivedFromAppCompose() public {
        bytes memory wrongCompose = _appCompose(
            "worker-0", _digestPinnedImage("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), ""
        );
        vm.expectRevert(bytes("worker image digest mismatch"));
        _reportData(registry, wrongCompose, publicKey);

        _expectRegisterRevert(
            bytes("worker image digest mismatch"), registry, new bytes(64), wrongCompose, publicKey, actionKey
        );
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

        _expectRegisterRevert(
            bytes("worker image digest mismatch"), registry, new bytes(64), smuggled, publicKey, actionKey
        );
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

        _expectRegisterRevert(
            bytes("quote report data mismatch"), registry, reportData, changedCompose, publicKey, actionKey
        );
    }

    function testRejectsThirdPartyRegistrationForWorker() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        _expectRegisterRevert(
            bytes("sender/action key mismatch"), registry, reportData, appCompose, publicKey, attacker
        );
    }

    function testRegisteredDeviceCannotSubmitSecondRegistration() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);

        vm.expectRevert(bytes("device already registered"));
        _reportData(registry, appCompose, publicKey);

        _expectRegisterRevert(
            bytes("device already registered"), registry, reportData, appCompose, publicKey, actionKey
        );
        assertEq(registry.registrationNonces(worker), 1);
        assertEq(registry.actionKeys(worker), actionKey);
    }

    function testImagePolicyIsFailClosed() public {
        DeviceRegistry unconfigured = new DeviceRegistry(keccak256("unconfigured deployment"));
        unconfigured.setTdxV4Attestation(address(verifier));

        vm.expectRevert(bytes("worker image policy not configured"));
        _reportData(unconfigured, appCompose, publicKey);

        _expectRegisterRevert(
            bytes("worker image policy not configured"), unconfigured, new bytes(64), appCompose, publicKey, actionKey
        );
    }

    function testImageRotationRevokesExistingRegistration() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);
        assertTrue(registry.isAuthorized(worker));

        registry.setExpectedWorkerImageDigest(sha256("next worker image"));
        assertFalse(registry.isAuthorized(worker));
    }

    function testRegisteredDeviceCanDeregisterOnlyItself() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);

        vm.expectRevert(bytes("action key not registered"));
        vm.prank(attacker);
        registry.deregisterDevice();
        assertTrue(registry.isAuthorized(worker));

        vm.prank(actionKey);
        registry.deregisterDevice();

        assertFalse(registry.isAuthorized(worker));
        assertEq(registry.registeredComposeHashes(worker), bytes32(0));
        assertEq(registry.registeredImageDigests(worker), bytes32(0));
        assertEq(registry.registeredWorkerPolicyHashes(worker), bytes32(0));
        assertEq(registry.registrationNonces(worker), 1);
        assertEq(registry.actionKeyForParticipant(worker), address(0));
        assertEq(registry.participantForActionKey(actionKey), address(0));

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
        vm.prank(actionKey);
        _register(registry, firstReportData, appCompose, publicKey);

        vm.prank(actionKey);
        registry.deregisterDevice();

        _expectRegisterRevert(
            bytes("quote report data mismatch"), registry, firstReportData, appCompose, publicKey, actionKey
        );

        bytes memory freshReportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
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

        _expectRegisterRevert(
            bytes("invalid attestation report data"), registry, hex"00", appCompose, publicKey, actionKey
        );
    }

    function testRejectsLegacyPackedVerifierOutput() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        MalformedTdxV4Attestation malformed = new MalformedTdxV4Attestation();
        malformed.setOutput(abi.encodePacked(bytes1(0), new bytes(48), reportData, bytes6(0)));
        registry.setTdxV4Attestation(address(malformed));

        _expectRegisterRevert(
            bytes("invalid attestation report data"), registry, reportData, appCompose, publicKey, actionKey
        );
    }

    function testVerifierRotationInvalidatesPreparedReportData() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        registry.setTdxV4Attestation(address(new MockTdxV4Attestation()));

        _expectRegisterRevert(
            bytes("quote report data mismatch"), registry, reportData, appCompose, publicKey, actionKey
        );
    }

    function testReportDataPreparationRequiresPublicKey() public {
        vm.expectRevert(bytes("public key required"));
        _reportData(registry, appCompose, new bytes(0));
    }

    function testEnrollmentRequiresLogicalParticipantSignature() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        bytes32 digest = registry.enrollmentDigest(worker, actionKey, appCompose);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(attackerPrivateKey, digest);
        bytes memory attackerAuthorization = abi.encodePacked(r, s, v);

        vm.expectRevert(bytes("invalid participant authorization"));
        _registerWithAuthorization(registry, reportData, appCompose, publicKey, actionKey, attackerAuthorization);
        assertFalse(registry.isAuthorized(worker));
    }

    function testEnrollmentRejectsMalleableHighSSignature() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        bytes memory authorization = _participantAuthorization(registry, appCompose);
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly ("memory-safe") {
            r := mload(add(authorization, 0x20))
            s := mload(add(authorization, 0x40))
            v := byte(0, mload(add(authorization, 0x60)))
        }
        uint256 curveOrder = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141;
        bytes memory malleable = abi.encodePacked(r, bytes32(curveOrder - uint256(s)), v == 27 ? uint8(28) : uint8(27));

        vm.expectRevert(bytes("non-canonical signature s"));
        _registerWithAuthorization(registry, reportData, appCompose, publicKey, actionKey, malleable);
    }

    function testPublicParticipantAccountCannotExerciseActionAuthority() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        _register(registry, reportData, appCompose, publicKey);

        vm.expectRevert(bytes("action key not registered"));
        vm.prank(worker);
        registry.deregisterDevice();
        assertTrue(registry.isAuthorized(worker));
    }

    function testRegisteredDeviceCannotRotateActionKeyWithoutDeregistering() public {
        bytes memory firstReportData = _reportData(registry, appCompose, publicKey);
        _register(registry, firstReportData, appCompose, publicKey);
        address registeredActionKey = actionKey;

        actionKey = makeAddr("rotated-worker-action-key");
        vm.expectRevert(bytes("device already registered"));
        _reportData(registry, appCompose, publicKey);
        _expectRegisterRevert(
            bytes("device already registered"), registry, new bytes(64), appCompose, publicKey, actionKey
        );

        assertEq(registry.registrationNonces(worker), 1);
        assertEq(registry.actionKeys(worker), registeredActionKey);
        assertEq(registry.participantForActionKey(registeredActionKey), worker);
        assertEq(registry.participantForActionKey(actionKey), address(0));
        vm.expectRevert(bytes("action key not registered"));
        vm.prank(actionKey);
        registry.deregisterDevice();
    }

    function testActionKeyRotationRejectsStaleParticipantAuthorization() public {
        address nextActionKey = makeAddr("next-worker-action-key");
        bytes32 staleDigest = registry.enrollmentDigest(worker, nextActionKey, appCompose);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(workerPrivateKey, staleDigest);
        bytes memory staleAuthorization = abi.encodePacked(r, s, v);

        bytes memory firstReportData = _reportData(registry, appCompose, publicKey);
        _register(registry, firstReportData, appCompose, publicKey);
        vm.prank(actionKey);
        registry.deregisterDevice();
        actionKey = nextActionKey;

        bytes memory freshReportData =
            registry.registrationReportData(worker, nextActionKey, PUBLIC_IP, BROKER_IP, publicKey, appCompose);
        vm.expectRevert(bytes("invalid participant authorization"));
        _registerWithAuthorization(registry, freshReportData, appCompose, publicKey, nextActionKey, staleAuthorization);
        assertEq(registry.actionKeys(worker), address(0));
        assertEq(registry.participantForActionKey(nextActionKey), address(0));
    }

    function testReportDataRejectsZeroActionKey() public {
        vm.expectRevert(bytes("invalid action key"));
        registry.registrationReportData(worker, address(0), PUBLIC_IP, BROKER_IP, publicKey, appCompose);
    }

    function testReportDataAndEnrollmentDigestBindActionKey() public view {
        address otherActionKey = address(0xA11CE);
        bytes memory reportData =
            registry.registrationReportData(worker, actionKey, PUBLIC_IP, BROKER_IP, publicKey, appCompose);
        bytes memory otherReportData =
            registry.registrationReportData(worker, otherActionKey, PUBLIC_IP, BROKER_IP, publicKey, appCompose);
        assertTrue(keccak256(reportData) != keccak256(otherReportData));
        assertTrue(
            registry.enrollmentDigest(worker, actionKey, appCompose)
                != registry.enrollmentDigest(worker, otherActionKey, appCompose)
        );
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
        return target.registrationReportData(worker, actionKey, PUBLIC_IP, BROKER_IP, key, compose);
    }

    function _register(DeviceRegistry target, bytes memory quote, bytes memory compose, bytes memory key) private {
        _registerFrom(target, quote, compose, key, actionKey);
    }

    function _registerFrom(
        DeviceRegistry target,
        bytes memory quote,
        bytes memory compose,
        bytes memory key,
        address sender
    ) private {
        bytes memory authorization = _participantAuthorization(target, compose);
        _registerWithAuthorization(target, quote, compose, key, sender, authorization);
    }

    function _registerWithAuthorization(
        DeviceRegistry target,
        bytes memory quote,
        bytes memory compose,
        bytes memory key,
        address sender,
        bytes memory authorization
    ) private {
        vm.prank(sender);
        target.registerDeviceWithAttestedAppCompose(
            quote, _events(), compose, worker, actionKey, PUBLIC_IP, BROKER_IP, key, authorization
        );
    }

    function _expectRegisterRevert(
        bytes memory reason,
        DeviceRegistry target,
        bytes memory quote,
        bytes memory compose,
        bytes memory key,
        address sender
    ) private {
        bytes memory authorization = _participantAuthorization(target, compose);
        vm.expectRevert(reason);
        _registerWithAuthorization(target, quote, compose, key, sender, authorization);
    }

    function _participantAuthorization(DeviceRegistry target, bytes memory compose) private returns (bytes memory) {
        bytes32 digest = target.enrollmentDigest(worker, actionKey, compose);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(workerPrivateKey, digest);
        return abi.encodePacked(r, s, v);
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
