// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {MockTdxV4Attestation} from "../src/attestation/MockTdxV4Attestation.sol";
import {Rtmr3Event} from "../src/attestation/Rtmr3Event.sol";

import {AggregatorSelection} from "../src/core/AggregatorSelection.sol";
import {GMStorage} from "../src/core/GMStorage.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";
import {WorkerAppAuthFactory} from "../src/kms/WorkerAppAuthFactory.sol";
import {WorkerAppAuth} from "../src/kms/WorkerAppAuth.sol";
import {IAppAuth} from "../src/kms/IAppAuth.sol";

contract KmsRegistrationStub {
    mapping(address => bool) public registeredApps;
    function registerApp(address appId) external { registeredApps[appId] = true; }
}

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

abstract contract DeviceRegistryAttestationFixture is Test {

    bool internal autoSnapshot = true;
    DeviceRegistry internal registry;
    MockTdxV4Attestation internal verifier;

    address internal worker;
    uint256 internal workerPrivateKey;
    address internal actionKey;
    address internal attacker;
    uint256 internal attackerPrivateKey;
    bytes internal publicKey;
    bytes32 internal imageDigest;
    bytes internal appCompose;

    string internal constant IMAGE_REPOSITORY = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker";
    string internal constant IMAGE_HEX = "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";
    string internal constant PUBLIC_IP = "https://worker.example";
    string internal constant BROKER_IP = "tcp://worker.example:5555";
    bytes32 internal constant SELLO_RECEIPT_KEY = 0x00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff;

    function setUp() public virtual {
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
        address[] memory roster = new address[](1);
        roster[0] = worker;
    }

    function makeAddrView() internal pure returns (address) { return address(0xA11CE); }

    function _configureKms() internal returns (WorkerAppAuthFactory factory, address appId) {
        factory = new WorkerAppAuthFactory(address(registry), address(new KmsRegistrationStub()));
        registry.setKmsAppFactory(address(factory), keccak256(bytes("trusted-kms")));
        vm.prank(worker);
        appId = factory.createApp(appCompose, bytes32(0));
    }

    function _registerKms(address appId, bytes memory provider, bytes memory reportData) internal {
        _registerKms(appId, provider, reportData, bytes(""));
    }

    function _registerKms(address appId, bytes memory provider, bytes memory reportData, bytes memory rejection) internal {
        Rtmr3Event[] memory events = new Rtmr3Event[](3);
        events[0] = Rtmr3Event(0x08000001, "compose-hash", abi.encodePacked(sha256(appCompose)));
        events[1] = Rtmr3Event(0x08000001, "app-id", abi.encodePacked(appId));
        events[2] = Rtmr3Event(0x08000001, "key-provider", provider);
        bytes memory authorization = _participantAuthorization(registry, appCompose);
        if (rejection.length != 0) vm.expectRevert(rejection);
        vm.prank(actionKey);
        registry.registerDeviceWithAttestedAppCompose(reportData, events, appCompose, worker,
            actionKey, PUBLIC_IP, BROKER_IP, publicKey, SELLO_RECEIPT_KEY, authorization);
    }

    function _reportData(DeviceRegistry target, bytes memory compose, bytes memory key)
        internal
        view
        returns (bytes memory)
    {
        return target.registrationReportData(worker, actionKey, PUBLIC_IP, BROKER_IP, key, SELLO_RECEIPT_KEY, compose);
    }

    function _register(DeviceRegistry target, bytes memory quote, bytes memory compose, bytes memory key) internal {
        _registerFrom(target, quote, compose, key, actionKey);
    }

    function _registerFrom(
        DeviceRegistry target,
        bytes memory quote,
        bytes memory compose,
        bytes memory key,
        address sender
    ) internal {
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
    ) internal {
        vm.prank(sender);
        target.registerDeviceWithAttestedAppCompose(
            quote, _events(), compose, worker, actionKey, PUBLIC_IP, BROKER_IP, key, SELLO_RECEIPT_KEY, authorization
        );
        if (autoSnapshot && target.isAuthorized(worker) && !target.runRosterFrozen()) {
            vm.prank(actionKey);
            target.snapshotRunRoster();
        }
    }

    function _expectRegisterRevert(
        bytes memory reason,
        DeviceRegistry target,
        bytes memory quote,
        bytes memory compose,
        bytes memory key,
        address sender
    ) internal {
        bytes memory authorization = _participantAuthorization(target, compose);
        vm.expectRevert(reason);
        _registerWithAuthorization(target, quote, compose, key, sender, authorization);
    }

    function _participantAuthorization(DeviceRegistry target, bytes memory compose) internal returns (bytes memory) {
        bytes32 digest = target.enrollmentDigest(worker, actionKey, SELLO_RECEIPT_KEY, compose);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(workerPrivateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _events() internal pure returns (Rtmr3Event[] memory events) {
        events = new Rtmr3Event[](1);
        events[0] = Rtmr3Event(0x08000001, "compose-hash", new bytes(32));
    }

    function _digestPinnedImage(string memory digestHex) internal pure returns (string memory) {
        return string.concat(IMAGE_REPOSITORY, "@sha256:", digestHex);
    }

    function _appCompose(string memory name, string memory image, string memory extraLine)
        internal
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

contract DeviceRegistryAttestationBindingTest is DeviceRegistryAttestationFixture {
    function testDerivesImageAndComposeIdentityOnChain() public view {
        (bytes32 composeHash, bytes32 derivedImageDigest) = registry.workloadIdentity(appCompose);
        assertEq(composeHash, sha256(appCompose));
        assertEq(derivedImageDigest, imageDigest);
        assertEq(
            registry.workerPolicyHash(appCompose), 0x40ac342d778573bc32ac684bacb0828285d30b120533f257d0509fd6ef8b5961
        );
    }

    function testRegistrationAndEnrollmentDomainsAreVersionSix() public view {
        assertEq(registry.REGISTRATION_REPORT_DATA_DOMAIN(), keccak256("MasterThesis.DeviceRegistry.registration.v6"));
        assertEq(
            registry.ENROLLMENT_TYPEHASH(),
            keccak256(
                "Enrollment(address participant,address actionKey,bytes32 composeHash,bytes32 selloReceiptKey,uint256 nonce)"
            )
        );
        assertEq(
            registry.domainSeparator(),
            keccak256(
                abi.encode(
                    registry.EIP712_DOMAIN_TYPEHASH(),
                    keccak256("VITA-FL DeviceRegistry"),
                    keccak256("6"),
                    block.chainid,
                    address(registry)
                )
            )
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
        assertEq(registry.selloReceiptKeys(worker), SELLO_RECEIPT_KEY);
        assertEq(registry.resolveAuthorizedParticipant(actionKey), worker);
        assertTrue(registry.runRosterFrozen());
        assertEq(registry.registeredRunMemberCount(), 1);

        (bool receiverAuthorized, string memory receiverPublicIp, bytes32 receiverKey) =
            registry.getSelloReceiver(worker);
        assertTrue(receiverAuthorized);
        assertEq(receiverPublicIp, PUBLIC_IP);
        assertEq(receiverKey, SELLO_RECEIPT_KEY);
    }

    function testSelloReceiverResolutionFailsClosed() public {
        vm.expectRevert(bytes("run roster not frozen"));
        registry.getSelloReceiver(worker);

        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        _register(registry, reportData, appCompose, publicKey);

        vm.expectRevert(bytes("not bootstrap worker"));
        registry.getSelloReceiver(attacker);
    }

    function testAnyUnselectedParticipantCanPrepareBoundEnrollment() public view {
        assertEq(registry.registrationReportData(attacker, makeAddrView(), PUBLIC_IP, BROKER_IP,
            publicKey, bytes32(0), appCompose).length, 64);
    }
    function testDeregistrationDeletesSelloReceiptKeyBeforeRosterFreeze() public {
        autoSnapshot = false;
        DeviceRegistry mutableRegistry = new DeviceRegistry(keccak256("mutable sello deployment"));
        mutableRegistry.setTdxV4Attestation(address(verifier));
        mutableRegistry.setExpectedWorkerImageDigest(imageDigest);
        mutableRegistry.setWorkerPolicyHashAllowed(mutableRegistry.workerPolicyHash(appCompose), true);
        address[] memory roster = new address[](2);
        roster[0] = worker;
        roster[1] = attacker;

        bytes memory reportData = _reportData(mutableRegistry, appCompose, publicKey);
        _register(mutableRegistry, reportData, appCompose, publicKey);
        assertEq(mutableRegistry.selloReceiptKeys(worker), SELLO_RECEIPT_KEY);
        assertFalse(mutableRegistry.runRosterFrozen());

        vm.prank(actionKey);
        mutableRegistry.deregisterDevice();
        assertEq(mutableRegistry.selloReceiptKeys(worker), bytes32(0));
    }

    function testRegistrationNeedsNoOwnerSelectedAddresses() public {
        assertFalse(registry.runRosterCommitted());
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        _register(registry, reportData, appCompose, publicKey);
        assertTrue(registry.isAuthorized(worker));
        assertTrue(registry.admissionPolicyLocked());
    }

    function testOwnerCannotPreselectOrBypassAttestation() public {
        (bool ok,) = address(registry).call(abi.encodeWithSignature("commitRunRoster(address[])", new address[](0)));
        assertFalse(ok);
        vm.expectRevert(bytes("attested registration required"));
        registry.authorizeAddress(attacker);
        assertFalse(registry.isAuthorized(attacker));
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

    function testEvaluationRunIdentifierIsVariableButGateOriginIsFixed() public view {
        bytes memory referenceCompose = _appCompose(
            "worker-0", _digestPinnedImage(IMAGE_HEX),
            "DFL_EVALUATION_GATE_URL: \\\"https://control.example\\\"\\n      DFL_EVALUATION_GATE_RUN_ID: \\\"\\\"\\n"
        );
        bytes memory activeCompose = _appCompose(
            "worker-0", _digestPinnedImage(IMAGE_HEX),
            "DFL_EVALUATION_GATE_URL: \\\"https://control.example\\\"\\n      DFL_EVALUATION_GATE_RUN_ID: \\\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\\"\\n"
        );
        bytes memory wrongOrigin = _appCompose(
            "worker-0", _digestPinnedImage(IMAGE_HEX),
            "DFL_EVALUATION_GATE_URL: \\\"https://other.example\\\"\\n      DFL_EVALUATION_GATE_RUN_ID: \\\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\\"\\n"
        );
        assertEq(registry.workerPolicyHash(referenceCompose), registry.workerPolicyHash(activeCompose));
        assertTrue(registry.workerPolicyHash(referenceCompose) != registry.workerPolicyHash(wrongOrigin));
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
        DeviceRegistry multiPolicyRegistry = new DeviceRegistry(keccak256("multi-policy deployment"));
        multiPolicyRegistry.setTdxV4Attestation(address(verifier));
        multiPolicyRegistry.setExpectedWorkerImageDigest(imageDigest);
        bytes32 inferencePolicy = multiPolicyRegistry.workerPolicyHash(inferenceRole);
        assertTrue(inferencePolicy != multiPolicyRegistry.workerPolicyHash(appCompose));
        multiPolicyRegistry.setWorkerPolicyHashAllowed(multiPolicyRegistry.workerPolicyHash(appCompose), true);
        multiPolicyRegistry.setWorkerPolicyHashAllowed(inferencePolicy, true);
        address[] memory roster = new address[](1);
        roster[0] = worker;
        assertEq(multiPolicyRegistry.allowedWorkerPolicyHashCount(), 2);

        bytes memory reportData = _reportData(multiPolicyRegistry, inferenceRole, publicKey);
        vm.prank(actionKey);
        _register(multiPolicyRegistry, reportData, inferenceRole, publicKey);
        assertTrue(multiPolicyRegistry.isAuthorized(worker));
        assertEq(multiPolicyRegistry.registeredWorkerPolicyHashes(worker), inferencePolicy);
    }

    function testAdmissionPolicyCannotChangeAfterRosterCommit() public {
        _register(registry, _reportData(registry, appCompose, publicKey), appCompose, publicKey);
        bytes32 policyHash = registry.workerPolicyHash(appCompose);
        bytes32 nextImageDigest = sha256("next worker image");
        vm.expectRevert(bytes("run admission policy is immutable"));
        registry.setWorkerPolicyHashAllowed(policyHash, false);
        vm.expectRevert(bytes("run admission policy is immutable"));
        registry.setExpectedWorkerImageDigest(nextImageDigest);
        MockTdxV4Attestation replacementVerifier = new MockTdxV4Attestation();
        vm.expectRevert(bytes("run admission policy is immutable"));
        registry.setTdxV4Attestation(address(replacementVerifier));
    }

    function testWorkerPolicySetIsFailClosed() public {
        DeviceRegistry unconfigured = new DeviceRegistry(keccak256("policy-less deployment"));
        unconfigured.setTdxV4Attestation(address(verifier));
        unconfigured.setExpectedWorkerImageDigest(imageDigest);
        address[] memory roster = new address[](1);
        roster[0] = worker;

        vm.expectRevert(bytes("worker policy set not configured"));
        _reportData(unconfigured, appCompose, publicKey);
    }

    function testCurrentRegistrationRequiresExactBoundEndpoints() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);

        assertTrue(
            registry.isDeviceRegistrationCurrent(
                worker, actionKey, PUBLIC_IP, BROKER_IP, publicKey, SELLO_RECEIPT_KEY, appCompose
            )
        );
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, actionKey, "https://replacement.example", BROKER_IP, publicKey, SELLO_RECEIPT_KEY, appCompose
            )
        );
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, actionKey, PUBLIC_IP, "tcp://replacement.example:5555", publicKey, SELLO_RECEIPT_KEY, appCompose
            )
        );
        assertFalse(
            registry.isDeviceRegistrationCurrent(
                worker, actionKey, PUBLIC_IP, BROKER_IP, publicKey, bytes32(uint256(1)), appCompose
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
        address[] memory roster = new address[](1);
        roster[0] = worker;

        vm.expectRevert(bytes("worker image policy not configured"));
        _reportData(unconfigured, appCompose, publicKey);

        _expectRegisterRevert(
            bytes("worker image policy not configured"), unconfigured, new bytes(64), appCompose, publicKey, actionKey
        );
    }

    function testFrozenRunMemberCannotDeregisterOrBeDeauthorized() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        vm.prank(actionKey);
        _register(registry, reportData, appCompose, publicKey);

        vm.expectRevert(bytes("action key not registered"));
        vm.prank(attacker);
        registry.deregisterDevice();
        assertTrue(registry.isAuthorized(worker));

        vm.expectRevert(bytes("run roster is immutable"));
        vm.prank(actionKey);
        registry.deregisterDevice();
        assertTrue(registry.isAuthorized(worker));
        vm.expectRevert(bytes("attested registration required"));
        registry.deauthorizeAddress(worker);
        address[] memory authorizedDevices = registry.getAuthorizedDevices();
        assertEq(authorizedDevices.length, 1);
        assertEq(authorizedDevices[0], worker);
    }

    function testRejectsMalformedVerifierOutput() public {
        MalformedTdxV4Attestation malformed = new MalformedTdxV4Attestation();
        malformed.setOutput(hex"00");
        DeviceRegistry malformedRegistry = new DeviceRegistry(keccak256("malformed verifier deployment"));
        malformedRegistry.setTdxV4Attestation(address(malformed));
        malformedRegistry.setExpectedWorkerImageDigest(imageDigest);
        malformedRegistry.setWorkerPolicyHashAllowed(malformedRegistry.workerPolicyHash(appCompose), true);
        address[] memory roster = new address[](1);
        roster[0] = worker;

        _expectRegisterRevert(
            bytes("invalid attestation report data"), malformedRegistry, hex"00", appCompose, publicKey, actionKey
        );
    }

    function testRejectsLegacyPackedVerifierOutput() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        MalformedTdxV4Attestation malformed = new MalformedTdxV4Attestation();
        malformed.setOutput(abi.encodePacked(bytes1(0), new bytes(48), reportData, bytes6(0)));
        DeviceRegistry malformedRegistry = new DeviceRegistry(keccak256("legacy verifier deployment"));
        malformedRegistry.setTdxV4Attestation(address(malformed));
        malformedRegistry.setExpectedWorkerImageDigest(imageDigest);
        malformedRegistry.setWorkerPolicyHashAllowed(malformedRegistry.workerPolicyHash(appCompose), true);
        address[] memory roster = new address[](1);
        roster[0] = worker;
        reportData = _reportData(malformedRegistry, appCompose, publicKey);

        _expectRegisterRevert(
            bytes("invalid attestation report data"), malformedRegistry, reportData, appCompose, publicKey, actionKey
        );
    }

    function testVerifierCannotRotateAfterRosterCommit() public {
        _register(registry, _reportData(registry, appCompose, publicKey), appCompose, publicKey);
        MockTdxV4Attestation replacementVerifier = new MockTdxV4Attestation();
        vm.expectRevert(bytes("run admission policy is immutable"));
        registry.setTdxV4Attestation(address(replacementVerifier));
    }

    function testReportDataPreparationRequiresPublicKey() public {
        vm.expectRevert(bytes("public key required"));
        _reportData(registry, appCompose, new bytes(0));
    }

    function testEnrollmentRequiresLogicalParticipantSignature() public {
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        bytes32 digest = registry.enrollmentDigest(worker, actionKey, SELLO_RECEIPT_KEY, appCompose);
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

    function testFrozenRosterRejectsActionKeyRotation() public {
        address nextActionKey = makeAddr("next-worker-action-key");
        bytes memory firstReportData = _reportData(registry, appCompose, publicKey);
        _register(registry, firstReportData, appCompose, publicKey);
        vm.expectRevert(bytes("run roster is immutable"));
        vm.prank(actionKey);
        registry.deregisterDevice();
        assertEq(registry.actionKeys(worker), actionKey);
        assertEq(registry.participantForActionKey(nextActionKey), address(0));
    }

    function testReportDataRejectsZeroActionKey() public {
        vm.expectRevert(bytes("invalid action key"));
        registry.registrationReportData(
            worker, address(0), PUBLIC_IP, BROKER_IP, publicKey, SELLO_RECEIPT_KEY, appCompose
        );
    }

    function testReportDataAndEnrollmentDigestBindActionAndSelloKeys() public view {
        address otherActionKey = address(0xA11CE);
        bytes32 otherSelloReceiptKey = bytes32(uint256(1));
        bytes memory reportData = registry.registrationReportData(
            worker, actionKey, PUBLIC_IP, BROKER_IP, publicKey, SELLO_RECEIPT_KEY, appCompose
        );
        bytes memory otherActionReportData = registry.registrationReportData(
            worker, otherActionKey, PUBLIC_IP, BROKER_IP, publicKey, SELLO_RECEIPT_KEY, appCompose
        );
        bytes memory otherSelloReportData = registry.registrationReportData(
            worker, actionKey, PUBLIC_IP, BROKER_IP, publicKey, otherSelloReceiptKey, appCompose
        );
        assertTrue(keccak256(reportData) != keccak256(otherActionReportData));
        assertTrue(keccak256(reportData) != keccak256(otherSelloReportData));
        assertTrue(
            registry.enrollmentDigest(worker, actionKey, SELLO_RECEIPT_KEY, appCompose)
                != registry.enrollmentDigest(worker, otherActionKey, SELLO_RECEIPT_KEY, appCompose)
        );
        assertTrue(
            registry.enrollmentDigest(worker, actionKey, SELLO_RECEIPT_KEY, appCompose)
                != registry.enrollmentDigest(worker, actionKey, otherSelloReceiptKey, appCompose)
        );
    }

    function testLegacySelectorCannotBypassAppComposeEvidence() public {
        vm.expectRevert(bytes("app compose evidence required"));
        registry.registerDeviceWithRtmr3EventsAndImageDigest(
            hex"00", new bytes[](0), bytes32(0), imageDigest, worker, PUBLIC_IP, BROKER_IP, publicKey
        );
    }

}

contract KmsAdmissionTest is DeviceRegistryAttestationFixture {
    function testKmsAppIsCreatedAutomaticallyWithoutParticipantAllowlist() public {
        (WorkerAppAuthFactory factory, address appId) = _configureKms();
        assertTrue(factory.isApprovedApp(appId, sha256(appCompose)));
        assertFalse(registry.isAuthorized(worker)); // Pre-boot policy is not membership.
        _registerKms(appId, bytes("trusted-kms"), _reportData(registry, appCompose, publicKey));
        assertTrue(registry.isAuthorized(worker));
        assertEq(registry.registeredAppIds(worker), appId);
    }

    function testKmsCurrentBootRejectsReplacementEvenForSameAppId() public {
        (, address appId) = _configureKms();
        IAppAuth.AppBootInfo memory boot;
        boot.appId = appId;
        boot.composeHash = sha256(appCompose);
        boot.tcbStatus = "UpToDate";
        (bool allowed,) = WorkerAppAuth(appId).isAppAllowed(boot);
        assertTrue(allowed);
        boot.composeHash = sha256("replacement image");
        (allowed,) = WorkerAppAuth(appId).isAppAllowed(boot);
        assertFalse(allowed);
        // Even the app operator cannot expand its release policy.
        vm.prank(worker);
        (bool changed,) = appId.call(abi.encodeWithSignature("addComposeHash(bytes32)", boot.composeHash));
        assertFalse(changed);
    }

    function testKmsRejectsDifferentAppIdentityAndUnacceptableTcb() public {
        (, address appId) = _configureKms();
        IAppAuth.AppBootInfo memory boot;
        boot.appId = attacker;
        boot.composeHash = sha256(appCompose);
        boot.tcbStatus = "UpToDate";
        (bool allowed,) = WorkerAppAuth(appId).isAppAllowed(boot);
        assertFalse(allowed);
        boot.appId = appId;
        boot.tcbStatus = "OutOfDate";
        (allowed,) = WorkerAppAuth(appId).isAppAllowed(boot);
        assertFalse(allowed);
    }

    function testKmsFactoryRejectsWrongImageBeforeKeyProvisioning() public {
        (WorkerAppAuthFactory factory,) = _configureKms();
        bytes memory other = _appCompose("replacement", _digestPinnedImage(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), "ROUND: \\\"1\\\"\\n");
        vm.expectRevert(bytes("worker image digest mismatch"));
        factory.createApp(other, bytes32(uint256(1)));
    }

    function testKmsEnrollmentRejectsOperatorControlledProvider() public {
        (, address appId) = _configureKms();
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        _registerKms(appId, bytes("operator-controlled-kms"), reportData, bytes("untrusted KMS provider"));
        assertFalse(registry.isAuthorized(worker));
    }

    function testKmsEnrollmentRejectsUnprotectedAppEvenWithApprovedImage() public {
        _configureKms();
        bytes memory reportData = _reportData(registry, appCompose, publicKey);
        _registerKms(attacker, bytes("trusted-kms"), reportData, bytes("unapproved KMS app policy"));
        assertFalse(registry.isAuthorized(worker));
    }

    function testKmsRestartReattestsSameKeysWithoutChangingLogicalIdentity() public {
        (, address appId) = _configureKms();
        bytes memory originalQuote = _reportData(registry, appCompose, publicKey);
        _registerKms(appId, bytes("trusted-kms"), originalQuote);
        vm.prank(actionKey);
        registry.snapshotRunRoster();
        bytes32 snapshot = registry.runRosterDigest();
        bytes memory nextQuote = _reportData(registry, appCompose, publicKey);
        _registerKms(appId, bytes("trusted-kms"), nextQuote);
        assertEq(registry.registrationNonces(worker), 2);
        assertEq(registry.actionKeys(worker), actionKey);
        assertEq(registry.runRosterDigest(), snapshot);
        assertEq(registry.getAuthorizedDevices().length, 1);
        _registerKms(appId, bytes("trusted-kms"), originalQuote, bytes("quote report data mismatch"));
    }

    function testRegistrationRemainsOpenAfterTrainingSnapshot() public {
        _register(registry, _reportData(registry, appCompose, publicKey), appCompose, publicKey);
        bytes32 snapshot = registry.runRosterDigest();
        worker = attacker;
        workerPrivateKey = attackerPrivateKey;
        actionKey = makeAddr("late-workload-action");
        _register(registry, _reportData(registry, appCompose, publicKey), appCompose, publicKey);
        assertTrue(registry.isAuthorized(attacker));
        assertEq(registry.getAuthorizedDevices().length, 2);
        assertEq(registry.getRunDevices().length, 1);
        assertEq(registry.runRosterDigest(), snapshot);
        assertFalse(registry.isRunParticipant(attacker));
    }

    function testSnapshotCannotOmitAnAlreadyRegisteredParticipant() public {
        autoSnapshot = false;
        _register(registry, _reportData(registry, appCompose, publicKey), appCompose, publicKey);
        address firstAction = actionKey;
        worker = attacker;
        workerPrivateKey = attackerPrivateKey;
        actionKey = makeAddr("second-workload-action");
        _register(registry, _reportData(registry, appCompose, publicKey), appCompose, publicKey);
        vm.prank(firstAction);
        registry.snapshotRunRoster();
        assertEq(registry.getRunRoster().length, 2);
        assertTrue(registry.committedRunMembers(attacker));
    }

    function testKmsRenewalPreservesEarnedScoreOnTheRealModelLedger() public {
        uint256 actionPrivate;
        (actionKey, actionPrivate) = makeAddrAndKey("stable-kms-action");
        (, address appId) = _configureKms();
        _registerKms(appId, bytes("trusted-kms"), _reportData(registry, appCompose, publicKey));
        vm.prank(actionKey);
        registry.snapshotRunRoster();
        // Deployment's initial address does not preselect the registered bootstrap role.
        AggregatorSelection selection = new AggregatorSelection(attacker);
        GMStorage ledger = new GMStorage(address(registry), address(selection), "initial-cid");
        selection.setGMStorageAddress(address(ledger));
        assertEq(selection.getCurrentAggregator(), worker);
        AggregationPolicy policy = new AggregationPolicy(address(ledger));
        ledger.setAggregationPolicyAddress(address(policy));
        policy.configureDefaultPolicy(1, 20);
        vm.prank(actionKey);
        ledger.openModelSubmissions(0);
        vm.prank(actionKey);
        ledger.closeModelSubmissions(0);
        (, , , , , uint32 accepted, , bytes32 algorithmHash, , , bytes32 policyHash, bytes32 inputRoot) =
            policy.getRoundPolicy(0);
        bytes32 modelHash = keccak256("model");
        bytes32 bundleHash = keccak256("bundle");
        bytes32 digest = policy.aggregationStatementDigest(0, worker, inputRoot, accepted,
            algorithmHash, policyHash, modelHash, bundleHash,
            keccak256(abi.encode("model-cid", "signature-cid", "bundle-cid")), 0);
        (uint8 v, bytes32 r, bytes32 sigS) = vm.sign(actionPrivate, digest);
        vm.prank(actionKey);
        ledger.finalizeRoundWithAggregation("model-cid", "signature-cid", "bundle-cid",
            modelHash, bundleHash, abi.encodePacked(r, sigS, v));
        assertEq(ledger.getContribution(worker), 1);
        _registerKms(appId, bytes("trusted-kms"), _reportData(registry, appCompose, publicKey));
        assertEq(ledger.getContribution(worker), 1);
        assertEq(registry.resolveAuthorizedParticipant(actionKey), worker);
    }

    function testOperatorFundingAmountsDoNotRequirePerParticipantPolicyApproval() public view {
        bytes memory first = _appCompose("worker", _digestPinnedImage(IMAGE_HEX),
            "ACTION_KEY_MIN_BALANCE_WEI: \\\"100\\\"\\n      ACTION_KEY_TARGET_BALANCE_WEI: \\\"200\\\"\\n");
        bytes memory second = _appCompose("worker", _digestPinnedImage(IMAGE_HEX),
            "ACTION_KEY_MIN_BALANCE_WEI: \\\"300\\\"\\n      ACTION_KEY_TARGET_BALANCE_WEI: \\\"400\\\"\\n");
        assertEq(registry.workerPolicyHash(first), registry.workerPolicyHash(second));
        assertTrue(sha256(first) != sha256(second));
    }

}
