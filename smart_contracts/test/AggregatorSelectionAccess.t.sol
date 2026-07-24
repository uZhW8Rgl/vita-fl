// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AggregatorSelection} from "../src/core/AggregatorSelection.sol";
import {GMStorage} from "../src/core/GMStorage.sol";

contract SelectionDeviceRegistryStub {
    mapping(address => bool) private authorized;
    mapping(address => bool) private known;
    address[] private authorizedDevices;

    function setAuthorized(address device, bool value) external {
        if (value && !known[device]) {
            known[device] = true;
            authorizedDevices.push(device);
        }
        authorized[device] = value;
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
}

contract AggregatorSelectionAccessTest is Test {
    AggregatorSelection private selection;
    GMStorage private gmStorage;
    SelectionDeviceRegistryStub private registry;

    address private aggregator;
    address private alternativeAggregator;
    address private reporterOne;
    address private reporterTwo;

    function setUp() public {
        aggregator = makeAddr("aggregator");
        alternativeAggregator = makeAddr("alternative-aggregator");
        reporterOne = makeAddr("reporter-one");
        reporterTwo = makeAddr("reporter-two");

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
    }

    function publishAndCompleteRound(address publisher, string memory suffix) private {
        vm.startPrank(publisher);
        gmStorage.closeModelSubmissions(gmStorage.getRound());
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            string.concat("model-", suffix), string.concat("signature-", suffix), string.concat("key-bundle-", suffix)
        );
        gmStorage.incrementRound();
        vm.stopPrank();
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

        vm.expectRevert(bytes("Caller is not an authorized participant"));
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

        vm.expectRevert(bytes("Aggregator is not authorized"));
        vm.prank(aggregator);
        selection.setSystemState("AGGREGATING");

        vm.expectRevert(bytes("Aggregator is not authorized"));
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

        vm.expectRevert(bytes("reporter not authorized"));
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
        gmStorage.recordModelSubmission(1, aggregator, keccak256("worker-round-one"));
        publishAndCompleteRound(replacement, "round-one");
        vm.prank(replacement);
        selection.triggerAggregatorSelection();

        assertEq(gmStorage.getRound(), 2);
        assertEq(selection.lastSelectionRound(), 2);
    }
}
