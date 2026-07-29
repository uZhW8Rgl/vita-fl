// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";
import {GMStorage} from "../src/core/GMStorage.sol";

contract ContributionDeviceRegistryStub {
    mapping(address => bool) private authorized;
    mapping(address => address) private participantActions;
    mapping(address => address) private actionParticipants;
    address[] private authorizedDevices;

    function setAuthorized(address device, bool value) external {
        if (value && !authorized[device]) {
            authorizedDevices.push(device);
        }
        authorized[device] = value;
    }

    function isAuthorized(address device) external view returns (bool) {
        return authorized[device];
    }

    function getAuthorizedDevices() external view returns (address[] memory) {
        return authorizedDevices;
    }

    function getDevice(address device) external view returns (bool, string memory, string memory, bytes memory) {
        return (authorized[device], "", "", bytes("publisher-public-key"));
    }

    function setActionKey(address participant, address actionKey) external {
        address previousAction = participantActions[participant];
        if (previousAction != address(0)) {
            delete actionParticipants[previousAction];
        }
        participantActions[participant] = actionKey;
        actionParticipants[actionKey] = participant;
    }

    function actionKeys(address participant) external view returns (address) {
        return participantActions[participant];
    }

    function resolveAuthorizedParticipant(address actionKey) external view returns (address) {
        address participant = actionParticipants[actionKey];
        require(participant != address(0), "action key not registered");
        require(authorized[participant], "participant is not authorized");
        return participant;
    }
}

contract ContributionAggregatorSelectionStub {
    address private aggregator;
    uint256 private selectionRound;

    function setAggregator(address newAggregator) external {
        aggregator = newAggregator;
    }

    function isAggregator(address candidate) external view returns (bool) {
        return candidate == aggregator;
    }

    function setSelectionRound(uint256 newSelectionRound) external {
        selectionRound = newSelectionRound;
    }

    function lastSelectionRound() external view returns (uint256) {
        return selectionRound;
    }
}

contract GMStorageContributionTest is Test {
    GMStorage private gmStorage;
    AggregationPolicy private aggregationPolicy;
    ContributionDeviceRegistryStub private registry;
    ContributionAggregatorSelectionStub private selection;
    address private aggregator;
    address private aggregatorAction;
    address private worker;
    address private workerAction;
    mapping(address => uint256) private privateKeys;

    struct SignedSubmission {
        bytes32 packageHash;
        bytes32 parentModelHash;
        uint256 workerNonce;
        bytes signature;
    }

    function setUp() public {
        uint256 aggregatorActionPrivateKey;
        uint256 workerPrivateKey;
        aggregator = makeAddr("aggregator");
        worker = makeAddr("worker");
        (aggregatorAction, aggregatorActionPrivateKey) = makeAddrAndKey("aggregator-action");
        (workerAction, workerPrivateKey) = makeAddrAndKey("worker-action");
        privateKeys[aggregator] = aggregatorActionPrivateKey;
        privateKeys[worker] = workerPrivateKey;

        registry = new ContributionDeviceRegistryStub();
        registry.setAuthorized(worker, true);
        registry.setAuthorized(aggregator, true);
        registry.setActionKey(aggregator, aggregatorAction);
        registry.setActionKey(worker, workerAction);

        selection = new ContributionAggregatorSelectionStub();
        selection.setAggregator(aggregator);

        gmStorage =
            new GMStorage(address(registry), address(selection), "initial-model", "initial-signature", aggregator);
        aggregationPolicy = new AggregationPolicy(address(gmStorage));
        aggregationPolicy.configureDefaultPolicy(1, 3600);
        gmStorage.setAggregationPolicyAddress(address(aggregationPolicy));
        vm.prank(aggregatorAction);
        gmStorage.openModelSubmissions(0);
    }

    function prepareSubmission(uint256 expectedRound, address target, bytes32 modelHash)
        private
        returns (SignedSubmission memory submission)
    {
        bytes32 packageHash = keccak256(abi.encodePacked("package:", modelHash));
        bytes32 parentModelHash = gmStorage.currentParentModelHash();
        uint256 workerNonce = gmStorage.hasSubmittedModel(expectedRound, target)
            ? gmStorage.modelSubmissionNonce(expectedRound, target)
            : gmStorage.workerSubmissionNonces(target);
        bytes32 digest = gmStorage.modelSubmissionDigest(
            expectedRound, target, aggregator, parentModelHash, modelHash, packageHash, workerNonce
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKeys[target], digest);
        submission = SignedSubmission(packageHash, parentModelHash, workerNonce, abi.encodePacked(r, s, v));
    }

    function callRecord(
        address sender,
        uint256 expectedRound,
        address target,
        bytes32 modelHash,
        SignedSubmission memory submission
    ) private {
        vm.prank(sender);
        gmStorage.recordModelSubmission(
            expectedRound,
            target,
            modelHash,
            submission.packageHash,
            submission.parentModelHash,
            submission.workerNonce,
            submission.signature
        );
    }

    function record(uint256 expectedRound, address target, bytes32 modelHash) private {
        callRecord(
            aggregatorAction, expectedRound, target, modelHash, prepareSubmission(expectedRound, target, modelHash)
        );
    }

    function expectRecordRevert(
        bytes memory reason,
        address sender,
        uint256 expectedRound,
        address target,
        bytes32 modelHash
    ) private {
        SignedSubmission memory submission = prepareSubmission(expectedRound, target, modelHash);
        vm.expectRevert(reason);
        callRecord(sender, expectedRound, target, modelHash, submission);
    }

    function completeCurrentRound() private {
        finalizeCurrentRound("global-model", "global-signature", "global-key-bundle");
        selection.setSelectionRound(1);
        vm.prank(aggregatorAction);
        gmStorage.openModelSubmissions(1);
    }

    function closeCurrentRound() private {
        uint256 currentRound = gmStorage.getRound();
        vm.prank(aggregatorAction);
        gmStorage.closeModelSubmissions(currentRound);
    }

    function finalizeCurrentRound(string memory model, string memory signatureCid, string memory keyBundle) private {
        uint256 currentRound = gmStorage.getRound();
        if (!gmStorage.modelSubmissionsClosed(currentRound)) {
            vm.prank(aggregatorAction);
            gmStorage.closeModelSubmissions(currentRound);
        }
        (,,,,, uint32 inputCount,, bytes32 algorithmHash,,, bytes32 policyHash, bytes32 inputRoot) =
            aggregationPolicy.getRoundPolicy(currentRound);
        bytes32 outputModelHash = keccak256(bytes(model));
        bytes32 outputBundleHash = keccak256(abi.encode(model, signatureCid, keyBundle, outputModelHash));
        bytes32 publicationHash = keccak256(abi.encode(model, signatureCid, keyBundle));
        bytes32 digest = aggregationPolicy.aggregationStatementDigest(
            currentRound,
            aggregator,
            inputRoot,
            inputCount,
            algorithmHash,
            policyHash,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            aggregationPolicy.aggregationNonces(aggregator)
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKeys[aggregator], digest);

        vm.prank(aggregatorAction);
        gmStorage.finalizeRoundWithAggregation(
            model, signatureCid, keyBundle, outputModelHash, outputBundleHash, abi.encodePacked(r, s, v)
        );
    }

    function testAggregatorConfirmationAwardsWorkerExactlyOnePoint() public {
        bytes32 modelHash = keccak256("round-zero-model");

        record(0, worker, modelHash);

        assertTrue(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.modelSubmissionHash(0, worker), modelHash);
        assertEq(gmStorage.modelSubmissionCount(0), 1);
        assertEq(gmStorage.getContribution(worker), 1);
    }

    function testIdenticalRetryIsIdempotent() public {
        bytes32 modelHash = keccak256("round-zero-model");

        record(0, worker, modelHash);
        record(0, worker, modelHash);

        assertTrue(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.modelSubmissionHash(0, worker), modelHash);
        assertEq(gmStorage.modelSubmissionCount(0), 1);
        assertEq(gmStorage.getContribution(worker), 1);
    }

    function testExactRetryDoesNotRequireSignatureReverificationOrMutateState() public {
        bytes32 modelHash = keccak256("round-zero-model");
        SignedSubmission memory submission = prepareSubmission(0, worker, modelHash);
        callRecord(aggregatorAction, 0, worker, modelHash, submission);

        submission.signature = hex"00";
        callRecord(aggregatorAction, 0, worker, modelHash, submission);

        assertEq(gmStorage.modelSubmissionCount(0), 1);
        assertEq(gmStorage.workerSubmissionNonces(worker), 1);
        assertEq(gmStorage.getContribution(worker), 1);
    }

    function testRetryWithChangedPackageHashReverts() public {
        bytes32 modelHash = keccak256("round-zero-model");
        SignedSubmission memory submission = prepareSubmission(0, worker, modelHash);
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
        submission.packageHash = keccak256("different-package");

        vm.expectRevert(bytes("Conflicting model submission package hash"));
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
    }

    function testRetryWithChangedParentHashReverts() public {
        bytes32 modelHash = keccak256("round-zero-model");
        SignedSubmission memory submission = prepareSubmission(0, worker, modelHash);
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
        submission.parentModelHash = keccak256("different-parent");

        vm.expectRevert(bytes("Conflicting model submission parent hash"));
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
    }

    function testFirstSubmissionRejectsWrongActionKeySignature() public {
        bytes32 modelHash = keccak256("round-zero-model");
        SignedSubmission memory submission = prepareSubmission(0, worker, modelHash);
        bytes32 digest = gmStorage.modelSubmissionDigest(
            0, worker, aggregator, submission.parentModelHash, modelHash, submission.packageHash, submission.workerNonce
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKeys[aggregator], digest);
        submission.signature = abi.encodePacked(r, s, v);

        vm.expectRevert(bytes("Invalid worker submission signature"));
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
        assertEq(gmStorage.workerSubmissionNonces(worker), 0);
    }

    function testFirstSubmissionRejectsSignatureFromReplacedWorkerActionKey() public {
        bytes32 modelHash = keccak256("round-zero-model");
        SignedSubmission memory submission = prepareSubmission(0, worker, modelHash);
        registry.setActionKey(worker, makeAddr("replacement-worker-action"));

        vm.expectRevert(bytes("Invalid worker submission signature"));
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.workerSubmissionNonces(worker), 0);
    }

    function testFirstSubmissionRejectsWrongNonce() public {
        bytes32 modelHash = keccak256("round-zero-model");
        SignedSubmission memory submission = prepareSubmission(0, worker, modelHash);
        submission.workerNonce = 7;
        bytes32 digest = gmStorage.modelSubmissionDigest(
            0, worker, aggregator, submission.parentModelHash, modelHash, submission.packageHash, submission.workerNonce
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKeys[worker], digest);
        submission.signature = abi.encodePacked(r, s, v);

        vm.expectRevert(bytes("Worker submission nonce mismatch"));
        callRecord(aggregatorAction, 0, worker, modelHash, submission);
    }

    function testRevokedAggregatorActionKeyCannotMutateProtocol() public {
        address replacementAction = makeAddr("replacement-aggregator-action");
        registry.setActionKey(aggregator, replacementAction);
        SignedSubmission memory submission = prepareSubmission(0, worker, keccak256("round-zero-model"));

        vm.expectRevert(bytes("action key not registered"));
        callRecord(aggregatorAction, 0, worker, keccak256("round-zero-model"), submission);
    }

    function testParentHashCommitsToModelSignatureAndKeyBundle() public {
        assertEq(gmStorage.currentParentModelHash(), keccak256(abi.encode("initial-model", "initial-signature", "")));

        finalizeCurrentRound("next-model", "next-signature", "next-key-bundle");

        assertEq(
            gmStorage.currentParentModelHash(), keccak256(abi.encode("next-model", "next-signature", "next-key-bundle"))
        );
    }

    function testDifferentHashForSameWorkerAndRoundReverts() public {
        bytes32 firstHash = keccak256("first-model");
        record(0, worker, firstHash);

        expectRecordRevert(
            bytes("Conflicting model submission hash"), aggregatorAction, 0, worker, keccak256("replacement-model")
        );

        assertEq(gmStorage.modelSubmissionHash(0, worker), firstHash);
        assertEq(gmStorage.getContribution(worker), 1);
    }

    function testWrongExpectedRoundReverts() public {
        expectRecordRevert(
            bytes("Submission round mismatch"), aggregatorAction, 1, worker, keccak256("future-round-model")
        );

        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testUnauthorizedWorkerCannotReceivePoint() public {
        address outsider = makeAddr("outsider");
        uint256 outsiderActionPrivateKey;
        address outsiderAction;
        (outsiderAction, outsiderActionPrivateKey) = makeAddrAndKey("outsider-action");
        privateKeys[outsider] = outsiderActionPrivateKey;
        registry.setActionKey(outsider, outsiderAction);

        expectRecordRevert(
            bytes("Worker is not authorized"), aggregatorAction, 0, outsider, keccak256("outsider-model")
        );

        assertFalse(gmStorage.hasSubmittedModel(0, outsider));
        assertEq(gmStorage.getContribution(outsider), 0);
    }

    function testZeroModelHashReverts() public {
        expectRecordRevert(bytes("Model hash is zero"), aggregatorAction, 0, worker, bytes32(0));

        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testAggregatorCannotAssignWorkerScoreToItself() public {
        expectRecordRevert(
            bytes("Current aggregator cannot receive worker score"),
            aggregatorAction,
            0,
            aggregator,
            keccak256("self-score")
        );

        assertFalse(gmStorage.hasSubmittedModel(0, aggregator));
        assertEq(gmStorage.getContribution(aggregator), 0);
    }

    function testWorkerCannotRecordItsOwnSubmission() public {
        bytes32 modelHash = keccak256("self-submitted-model");
        expectRecordRevert(bytes("Caller is not the current aggregator"), workerAction, 0, worker, modelHash);

        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testSubmissionForFinalizedRoundReverts() public {
        finalizeCurrentRound("global-model", "global-signature", "global-key-bundle");
        selection.setSelectionRound(1);

        expectRecordRevert(bytes("Submission round mismatch"), aggregatorAction, 0, worker, keccak256("late-model"));

        assertEq(gmStorage.modelSubmissionCount(0), 0);
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testSubmissionWindowClosureRejectsNewModelsButAllowsIdenticalRetry() public {
        bytes32 modelHash = keccak256("accepted-model");
        record(0, worker, modelHash);
        closeCurrentRound();

        record(0, worker, modelHash);

        address secondWorker = makeAddr("second-worker");
        uint256 secondWorkerActionPrivateKey;
        address secondWorkerAction;
        (secondWorkerAction, secondWorkerActionPrivateKey) = makeAddrAndKey("second-worker-action");
        privateKeys[secondWorker] = secondWorkerActionPrivateKey;
        registry.setAuthorized(secondWorker, true);
        registry.setActionKey(secondWorker, secondWorkerAction);
        expectRecordRevert(
            bytes("Model submissions are closed"), aggregatorAction, 0, secondWorker, keccak256("late-model")
        );

        assertTrue(gmStorage.modelSubmissionsClosed(0));
        assertEq(gmStorage.modelSubmissionCount(0), 1);
        assertEq(gmStorage.getContribution(worker), 1);
        assertEq(gmStorage.getContribution(secondWorker), 0);
    }

    function testOnlyActiveAuthorizedAggregatorCanCloseSubmissionWindow() public {
        vm.expectRevert(bytes("Caller is not the current aggregator"));
        vm.prank(workerAction);
        gmStorage.closeModelSubmissions(0);

        registry.setAuthorized(aggregator, false);
        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(aggregatorAction);
        gmStorage.closeModelSubmissions(0);

        assertFalse(gmStorage.modelSubmissionsClosed(0));
    }

    function testDeauthorizedAggregatorCannotRecordSubmission() public {
        registry.setAuthorized(aggregator, false);

        expectRecordRevert(
            bytes("participant is not authorized"), aggregatorAction, 0, worker, keccak256("worker-model")
        );

        assertEq(gmStorage.modelSubmissionCount(0), 0);
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testWorkerCanReceiveOneNewPointInNextRound() public {
        record(0, worker, keccak256("round-zero-model"));
        completeCurrentRound();
        record(1, worker, keccak256("round-one-model"));

        assertTrue(gmStorage.hasSubmittedModel(0, worker));
        assertTrue(gmStorage.hasSubmittedModel(1, worker));
        assertEq(gmStorage.getContribution(worker), 2);
        assertEq(gmStorage.getContribution(aggregator), 1);
    }

    function testWorkerCallableSubmissionEntryPointsAreUnavailable() public {
        (bool submitModelSuccess,) =
            address(gmStorage).call(abi.encodeWithSignature("submitModel(bytes32)", keccak256("worker-model")));

        address[] memory devices = new address[](1);
        devices[0] = worker;
        (bool incrementContributionSuccess,) =
            address(gmStorage).call(abi.encodeWithSignature("incrementContribution(address[])", devices));

        assertFalse(submitModelSuccess);
        assertFalse(incrementContributionSuccess);
        assertEq(gmStorage.getContribution(worker), 0);
    }
}
