// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AggregatorSelection} from "../src/core/AggregatorSelection.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";
import {GMStorage} from "../src/core/GMStorage.sol";
import {ActionKeyTest} from "./helpers/ActionKeyTest.sol";

contract SelectionDeviceRegistryStub {
    mapping(address => bool) private authorized;
    mapping(address => bool) private known;
    mapping(address => address) private participantActions;
    mapping(address => address) private actionParticipants;
    address[] private authorizedDevices;

    function setAuthorized(address device, bool value) external {
        if (value && !known[device]) {
            known[device] = true;
            authorizedDevices.push(device);
        }
        authorized[device] = value;
        if (participantActions[device] == address(0)) {
            participantActions[device] = device;
            actionParticipants[device] = device;
        }
    }

    function isAuthorized(address device) external view returns (bool) {
        return authorized[device];
    }

    function getAuthorizedDevices() external view returns (address[] memory) {
        uint256 count;
        for (uint256 i = 0; i < authorizedDevices.length; i++) {
            if (authorized[authorizedDevices[i]]) {
                count++;
            }
        }
        address[] memory activeDevices = new address[](count);
        uint256 cursor;
        for (uint256 i = 0; i < authorizedDevices.length; i++) {
            if (authorized[authorizedDevices[i]]) {
                activeDevices[cursor] = authorizedDevices[i];
                cursor++;
            }
        }
        return activeDevices;
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

contract AggregatorSelectionAccessTest is ActionKeyTest {
    AggregatorSelection private selection;
    GMStorage private gmStorage;
    AggregationPolicy private aggregationPolicy;
    SelectionDeviceRegistryStub private registry;

    address private aggregator;
    address private alternativeAggregator;
    address private reporterOne;
    address private reporterTwo;

    function setUp() public {
        aggregator = _makeActionParticipant("aggregator");
        alternativeAggregator = _makeActionParticipant("alternative-aggregator");
        reporterOne = _makeActionParticipant("reporter-one");
        reporterTwo = _makeActionParticipant("reporter-two");

        registry = new SelectionDeviceRegistryStub();
        registry.setAuthorized(aggregator, true);
        registry.setAuthorized(alternativeAggregator, true);
        registry.setAuthorized(reporterOne, true);
        registry.setAuthorized(reporterTwo, true);

        selection = new AggregatorSelection();
        selection.setCurrentAggregator(aggregator);
        gmStorage =
            new GMStorage(address(registry), address(selection), "initial-model", "initial-signature", aggregator);
        selection.setGMStorageAddress(address(gmStorage));
        aggregationPolicy = new AggregationPolicy(address(gmStorage));
        aggregationPolicy.configureDefaultPolicy(1, 3600);
        gmStorage.setAggregationPolicyAddress(address(aggregationPolicy));
        vm.prank(aggregator);
        gmStorage.openModelSubmissions(0);
    }

    function publishAndCompleteRound(address publisher, string memory suffix) private {
        uint256 currentRound = gmStorage.getRound();
        string memory model = string.concat("model-", suffix);
        string memory signatureCid = string.concat("signature-", suffix);
        string memory keyBundle = string.concat("key-bundle-", suffix);
        vm.prank(publisher);
        gmStorage.closeModelSubmissions(currentRound);
        (,,,,, uint32 inputCount,, bytes32 algorithmHash, bytes32 policyHash, bytes32 inputRoot) =
            aggregationPolicy.getRoundPolicy(currentRound);
        bytes32 outputModelHash = keccak256(bytes(model));
        bytes32 outputBundleHash = keccak256(abi.encode(model, signatureCid, keyBundle, outputModelHash));
        bytes32 publicationHash = keccak256(abi.encode(model, signatureCid, keyBundle));
        bytes32 digest = aggregationPolicy.aggregationStatementDigest(
            currentRound,
            publisher,
            inputRoot,
            inputCount,
            algorithmHash,
            policyHash,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            aggregationPolicy.aggregationNonces(publisher)
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(actionPrivateKeys[publisher], digest);

        vm.prank(publisher);
        gmStorage.finalizeRoundWithAggregation(
            model, signatureCid, keyBundle, outputModelHash, outputBundleHash, abi.encodePacked(r, s, v)
        );
    }

    function testCurrentAggregatorCanSelectAfterCompletedRound() public {
        publishAndCompleteRound(aggregator, "round-zero");

        vm.prank(aggregator);
        selection.triggerAggregatorSelection();

        assertEq(selection.lastSelectionRound(), 1);
        assertEq(gmStorage.getRound(), 1);
    }

    function testSelectionCannotRunBeforeGMStorageRoundAdvanced() public {
        vm.expectRevert(bytes("GMStorage round not advanced"));
        vm.prank(aggregator);
        selection.triggerAggregatorSelection();
    }

    function testAuthorizedNonAggregatorCanRecoverSelectionGapAfterCompletedRound() public {
        publishAndCompleteRound(aggregator, "round-zero");
        registry.setAuthorized(aggregator, false);

        vm.prank(reporterOne);
        selection.triggerAggregatorSelection();

        assertEq(selection.lastSelectionRound(), 1);
        assertEq(selection.system_state(), "TRAINING");
        assertTrue(selection.getCurrentAggregator() != address(0));
    }

    function testUnauthorizedCallerCannotTriggerRegularSelection() public {
        publishAndCompleteRound(aggregator, "round-zero");
        address outsider = makeAddr("outsider");

        vm.expectRevert(bytes("action key not registered"));
        vm.prank(outsider);
        selection.triggerAggregatorSelection();

        assertEq(selection.lastSelectionRound(), 0);
    }

    function testAbortedRoundAloneIsNotACompletedRound() public {
        vm.prank(address(selection));
        gmStorage.abortRound(aggregator);

        vm.expectRevert(bytes("Previous GMStorage round not completed"));
        vm.prank(aggregator);
        selection.triggerAggregatorSelection();

        assertEq(gmStorage.getRound(), 1);
        assertFalse(gmStorage.roundCompleted(0));
        assertEq(selection.lastSelectionRound(), 0);
    }

    function testTimeoutReportsAreScopedByRoundAggregatorAndReporter() public {
        selection.setTimeoutReportThresholdPercent(100);

        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);

        assertTrue(selection.timeoutReported(0, aggregator, reporterOne));
        assertEq(selection.timeoutReportCount(0, aggregator), 1);
        assertEq(selection.timeoutEligibleReporterCount(0, aggregator), 3);
        assertEq(selection.timeoutRequiredReportCount(0, aggregator), 3);

        vm.expectRevert(bytes("timeout already reported"));
        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);
    }

    function testTimeoutReportCannotAbortAnActiveSubmissionWindow() public {
        publishAndCompleteRound(aggregator, "round-zero");
        vm.prank(aggregator);
        selection.triggerAggregatorSelection();
        address selected = selection.getCurrentAggregator();
        vm.prank(selected);
        gmStorage.openModelSubmissions(1);
        address reporter = reporterOne == selected ? reporterTwo : reporterOne;

        vm.expectRevert(bytes("submission window is still active"));
        vm.prank(reporter);
        selection.reportAggregatorTimeout(1, selected);

        assertEq(selection.timeoutReportCount(1, selected), 0);
        assertFalse(selection.roundAborted(1));
    }

    function testStaleAggregatorExpectationRevertsWithoutCounting() public {
        vm.expectRevert(bytes("timeout aggregator mismatch"));
        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, alternativeAggregator);

        assertFalse(selection.timeoutReported(0, aggregator, reporterOne));
        assertFalse(selection.timeoutReported(0, alternativeAggregator, reporterOne));
        assertEq(selection.timeoutReportCount(0, aggregator), 0);
        assertEq(selection.timeoutReportCount(0, alternativeAggregator), 0);
    }

    function testOwnerCannotOverrideSelectedAggregatorAfterGMStorageConfiguration() public {
        vm.expectRevert(bytes("aggregator bootstrap is closed"));
        selection.setCurrentAggregator(alternativeAggregator);

        assertEq(selection.getCurrentAggregator(), aggregator);
        assertEq(selection.lastSelectionRound(), 0);
    }

    function testOwnerCannotResetSelectionRoundByReconfiguringGMStorage() public {
        publishAndCompleteRound(aggregator, "round-zero");

        vm.expectRevert(bytes("GMStorage already configured"));
        selection.setGMStorageAddress(address(gmStorage));

        assertEq(selection.lastSelectionRound(), 0);
        assertEq(gmStorage.getRound(), 1);
    }

    function testExternalStateSetterRejectsUnknownState() public {
        vm.expectRevert(bytes("invalid system state"));
        vm.prank(aggregator);
        selection.setSystemState("ARBITRARY");

        assertEq(selection.system_state(), "TRAINING");
    }

    function testAggregatorStateMutationUsesActionKeyButKeepsLogicalIdentity() public {
        address teeAction = makeAddr("aggregator-tee-action");
        registry.setActionKey(aggregator, teeAction);

        vm.prank(teeAction);
        selection.setSystemState("AGGREGATING");
        assertEq(selection.system_state(), "AGGREGATING");
        assertEq(selection.getCurrentAggregator(), aggregator);

        vm.expectRevert(bytes("action key not registered"));
        vm.prank(aggregator);
        selection.setSystemState("TRAINING");
    }

    function testTimeoutReportFromActionKeyIsAttributedToLogicalReporter() public {
        address reporterAction = makeAddr("reporter-one-tee-action");
        registry.setActionKey(reporterOne, reporterAction);

        vm.prank(reporterAction);
        selection.reportAggregatorTimeout(0, aggregator);

        assertTrue(selection.timeoutReported(0, aggregator, reporterOne));
        assertFalse(selection.timeoutReported(0, aggregator, reporterAction));
    }

    function testCompletedRoundAggregatorCannotMutateNextRoundBeforeSelection() public {
        publishAndCompleteRound(aggregator, "round-zero");

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        selection.setSystemState("AGGREGATING");

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        selection.setBrokerEndpoint("stale-endpoint");

        assertEq(selection.system_state(), "TRAINING");
        assertEq(selection.getBrokerEndpoint(), "test_endpoint");
    }

    function testDeauthorizedSelectedAggregatorCannotMutateStateOrEndpoint() public {
        registry.setAuthorized(aggregator, false);

        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(aggregator);
        selection.setSystemState("AGGREGATING");

        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(aggregator);
        selection.setBrokerEndpoint("unauthorized-endpoint");
    }

    function testStaleRoundExpectationRevertsWithoutCounting() public {
        publishAndCompleteRound(aggregator, "round-zero");

        vm.expectRevert(bytes("timeout round mismatch"));
        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);

        assertFalse(selection.timeoutReported(0, aggregator, reporterOne));
        assertFalse(selection.timeoutReported(1, aggregator, reporterOne));
        assertEq(selection.timeoutReportCount(0, aggregator), 0);
        assertEq(selection.timeoutReportCount(1, aggregator), 0);
    }

    function testTimeoutCannotTargetSuccessfulAggregatorBeforeNextRoundSelection() public {
        selection.setTimeoutReportThresholdPercent(1);
        publishAndCompleteRound(aggregator, "round-zero");

        vm.expectRevert(bytes("aggregator not selected for current round"));
        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(1, aggregator);

        assertFalse(selection.timeoutReported(1, aggregator, reporterOne));
        assertEq(selection.timeoutReportCount(1, aggregator), 0);
        assertFalse(selection.roundAborted(1));
        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getContribution(aggregator), 1);
    }

    function testTimeoutQuorumDoesNotShrinkAfterRegistryContraction() public {
        selection.setTimeoutReportThresholdPercent(100);

        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);

        registry.setAuthorized(alternativeAggregator, false);

        vm.prank(reporterTwo);
        selection.reportAggregatorTimeout(0, aggregator);

        assertEq(selection.timeoutEligibleReporterCount(0, aggregator), 3);
        assertEq(selection.timeoutRequiredReportCount(0, aggregator), 3);
        assertEq(selection.timeoutReportCount(0, aggregator), 2);
        assertEq(selection.getCurrentAggregator(), aggregator);
        assertEq(gmStorage.getRound(), 0);
        assertFalse(selection.roundAborted(0));
    }

    function testTimeoutReporterMembershipCannotBeSubstitutedAfterSnapshot() public {
        selection.setTimeoutReportThresholdPercent(100);
        address laterReporter = makeAddr("later-reporter");

        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);

        registry.setAuthorized(laterReporter, true);
        vm.expectRevert(bytes("reporter not in timeout snapshot"));
        vm.prank(laterReporter);
        selection.reportAggregatorTimeout(0, aggregator);

        assertTrue(selection.timeoutReporterEligible(0, aggregator, reporterOne));
        assertFalse(selection.timeoutReporterEligible(0, aggregator, laterReporter));
        assertFalse(selection.timeoutReported(0, aggregator, laterReporter));
        assertEq(selection.timeoutReportCount(0, aggregator), 1);
        assertEq(selection.timeoutEligibleReporterCount(0, aggregator), 3);
    }

    function testRevokedSnapshotMemberCannotReportButDoesNotShrinkQuorum() public {
        selection.setTimeoutReportThresholdPercent(100);

        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);
        registry.setAuthorized(reporterTwo, false);

        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(reporterTwo);
        selection.reportAggregatorTimeout(0, aggregator);

        assertTrue(selection.timeoutReporterEligible(0, aggregator, reporterTwo));
        assertFalse(selection.timeoutReported(0, aggregator, reporterTwo));
        assertEq(selection.timeoutEligibleReporterCount(0, aggregator), 3);
        assertEq(selection.timeoutRequiredReportCount(0, aggregator), 3);
        assertEq(selection.timeoutReportCount(0, aggregator), 1);
    }

    function testTimeoutSelectionAdvancesAndConsumesSelectionRoundMarker() public {
        vm.prank(reporterOne);
        selection.reportAggregatorTimeout(0, aggregator);
        vm.prank(reporterTwo);
        selection.reportAggregatorTimeout(0, aggregator);

        address replacement = selection.getCurrentAggregator();
        assertTrue(replacement != aggregator);
        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getCompletedRoundCount(), 0);
        assertTrue(selection.roundAborted(0));
        assertEq(selection.timeoutReportCount(0, aggregator), 2);
        assertEq(selection.timeoutEligibleReporterCount(0, aggregator), 3);
        assertEq(selection.timeoutRequiredReportCount(0, aggregator), 2);
        assertEq(selection.lastSelectionRound(), 1);
        assertFalse(gmStorage.roundCompleted(0));
        assertTrue(gmStorage.penaltyApplied(0, aggregator, keccak256(bytes("aggregator_timeout_consensus"))));

        vm.expectRevert(bytes("GMStorage round not advanced"));
        vm.prank(replacement);
        selection.triggerAggregatorSelection();

        vm.prank(replacement);
        gmStorage.openModelSubmissions(1);
        _recordSignedModelSubmission(gmStorage, replacement, replacement, aggregator, 1, keccak256("worker-round-one"));
        publishAndCompleteRound(replacement, "round-one");
        vm.prank(replacement);
        selection.triggerAggregatorSelection();

        assertEq(gmStorage.getRound(), 2);
        assertEq(selection.lastSelectionRound(), 2);
    }
}
