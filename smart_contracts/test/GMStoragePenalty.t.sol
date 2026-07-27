// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {GMStorage} from "../src/core/GMStorage.sol";
import {ActionKeyTest} from "./helpers/ActionKeyTest.sol";

contract PenaltyDeviceRegistryStub {
    mapping(address => bool) private authorized;
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

    function actionKeys(address participant) external pure returns (address) {
        return participant;
    }

    function resolveAuthorizedParticipant(address actionKey) external view returns (address) {
        require(authorized[actionKey], "participant is not authorized");
        return actionKey;
    }
}

contract PenaltyAggregatorSelectionStub {
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

contract GMStoragePenaltyTest is ActionKeyTest {
    GMStorage private gmStorage;
    PenaltyDeviceRegistryStub private registry;
    PenaltyAggregatorSelectionStub private selection;

    address private aggregator;
    address private missingWorker;
    address private submittingWorker;

    function setUp() public {
        aggregator = _makeActionParticipant("aggregator");
        missingWorker = _makeActionParticipant("missing-worker");
        submittingWorker = _makeActionParticipant("submitting-worker");

        registry = new PenaltyDeviceRegistryStub();
        registry.setAuthorized(aggregator, true);
        registry.setAuthorized(missingWorker, true);
        registry.setAuthorized(submittingWorker, true);

        selection = new PenaltyAggregatorSelectionStub();
        selection.setAggregator(aggregator);

        gmStorage =
            new GMStorage(address(registry), address(selection), "initial-model", "initial-signature", aggregator);

        _recordSignedModelSubmission(
            gmStorage, aggregator, aggregator, missingWorker, 0, keccak256("missing-worker-round-zero")
        );
        _recordSignedModelSubmission(
            gmStorage, aggregator, aggregator, submittingWorker, 0, keccak256("submitting-worker-round-zero")
        );
        vm.startPrank(aggregator);
        gmStorage.closeModelSubmissions(0);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            "global-model-round-zero", "global-signature-round-zero", "global-key-bundle-round-zero"
        );
        gmStorage.incrementRound();
        vm.stopPrank();
        selection.setSelectionRound(1);
    }

    function singleTarget(address target) private pure returns (address[] memory targets) {
        targets = new address[](1);
        targets[0] = target;
    }

    function closeCurrentRound() private {
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(1);
    }

    function testAggregatorCanPenalizeAuthorizedMissingWorker() public {
        closeCurrentRound();
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "missed_model_deadline");

        assertEq(gmStorage.getContribution(missingWorker), 0);
        assertTrue(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }

    function testAggregatorCannotUseArbitraryPenaltyReason() public {
        closeCurrentRound();
        vm.expectRevert(bytes("Invalid worker penalty reason"));
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "arbitrary_reason");
    }

    function testAggregatorCannotPenalizeCurrentAggregator() public {
        closeCurrentRound();
        vm.expectRevert(bytes("Cannot apply worker penalty to aggregator"));
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(aggregator), "missed_model_deadline");
    }

    function testUnknownZeroScoreOutsiderIsSkipped() public {
        address outsider = makeAddr("outsider");

        closeCurrentRound();
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(outsider), "missed_model_deadline");

        assertEq(gmStorage.getContribution(outsider), 0);
        assertFalse(gmStorage.penaltyApplied(1, outsider, keccak256(bytes("missed_model_deadline"))));
    }

    function testDeregisteredScoreBearingWorkerCanStillBePenalized() public {
        registry.setAuthorized(missingWorker, false);

        closeCurrentRound();
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "missed_model_deadline");

        assertEq(gmStorage.getContribution(missingWorker), 0);
        assertTrue(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }

    function testUnknownOutsiderDoesNotRevertPenaltyBatch() public {
        address outsider = makeAddr("outsider");
        address[] memory targets = new address[](2);
        targets[0] = outsider;
        targets[1] = missingWorker;

        closeCurrentRound();
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, targets, "missed_model_deadline");

        assertEq(gmStorage.getContribution(outsider), 0);
        assertFalse(gmStorage.penaltyApplied(1, outsider, keccak256(bytes("missed_model_deadline"))));
        assertEq(gmStorage.getContribution(missingWorker), 0);
        assertTrue(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }

    function testAggregatorCannotPenalizeWorkerThatSubmittedCurrentRound() public {
        _recordSignedModelSubmission(
            gmStorage, aggregator, aggregator, submittingWorker, 1, keccak256("submitting-worker-round-one")
        );

        closeCurrentRound();
        vm.expectRevert(bytes("Cannot penalize submitted model"));
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(submittingWorker), "missed_model_deadline");

        assertEq(gmStorage.getContribution(submittingWorker), 2);
    }

    function testAggregatorSelectionCanOnlyPenalizeCurrentAggregatorOnTimeout() public {
        vm.prank(address(selection));
        gmStorage.penalizeContribution(1, aggregator, singleTarget(aggregator), "aggregator_timeout_consensus");

        assertEq(gmStorage.getContribution(aggregator), 0);
        assertTrue(gmStorage.penaltyApplied(1, aggregator, keccak256(bytes("aggregator_timeout_consensus"))));
    }

    function testAggregatorSelectionCannotUseWrongReason() public {
        vm.expectRevert(bytes("Invalid timeout penalty reason"));
        vm.prank(address(selection));
        gmStorage.penalizeContribution(1, aggregator, singleTarget(aggregator), "missed_model_deadline");
    }

    function testAggregatorSelectionCannotPenalizeDifferentTarget() public {
        vm.expectRevert(bytes("Timeout target is not expected aggregator"));
        vm.prank(address(selection));
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "aggregator_timeout_consensus");
    }

    function testAggregatorSelectionCannotPenalizeMultipleTargets() public {
        address[] memory targets = new address[](2);
        targets[0] = aggregator;
        targets[1] = missingWorker;

        vm.expectRevert(bytes("Timeout penalty requires one target"));
        vm.prank(address(selection));
        gmStorage.penalizeContribution(1, aggregator, targets, "aggregator_timeout_consensus");
    }

    function testOutsiderCannotPenalize() public {
        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(makeAddr("outsider"));
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "missed_model_deadline");
    }

    function testStalePenaltyRoundRevertsWithoutMutation() public {
        vm.expectRevert(bytes("Penalty round mismatch"));
        vm.prank(aggregator);
        gmStorage.penalizeContribution(0, aggregator, singleTarget(missingWorker), "missed_model_deadline");

        assertEq(gmStorage.getContribution(missingWorker), 1);
        assertFalse(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }

    function testStalePenaltyAggregatorRevertsWithoutMutation() public {
        address replacement = makeAddr("replacement-aggregator");
        registry.setAuthorized(replacement, true);
        selection.setAggregator(replacement);

        vm.expectRevert(bytes("Penalty aggregator mismatch"));
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "missed_model_deadline");

        assertEq(gmStorage.getContribution(missingWorker), 1);
        assertFalse(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }

    function testWorkerPenaltyAfterPublicationReverts() public {
        _recordSignedModelSubmission(
            gmStorage, aggregator, aggregator, submittingWorker, 1, keccak256("submitting-worker-round-one")
        );
        vm.startPrank(aggregator);
        gmStorage.closeModelSubmissions(1);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            "global-model-round-one", "global-signature-round-one", "global-key-bundle-round-one"
        );

        vm.expectRevert(bytes("Worker penalties closed after publication"));
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "missed_model_deadline");
        vm.stopPrank();

        assertEq(gmStorage.getContribution(missingWorker), 1);
        assertFalse(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }

    function testWorkerPenaltyRequiresClosedSubmissionWindow() public {
        vm.expectRevert(bytes("Model submissions are still open"));
        vm.prank(aggregator);
        gmStorage.penalizeContribution(1, aggregator, singleTarget(missingWorker), "missed_model_deadline");

        assertEq(gmStorage.getContribution(missingWorker), 1);
        assertFalse(gmStorage.penaltyApplied(1, missingWorker, keccak256(bytes("missed_model_deadline"))));
    }
}
