// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {GMStorage} from "../src/core/GMStorage.sol";

contract ContributionDeviceRegistryStub {
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
    ContributionDeviceRegistryStub private registry;
    ContributionAggregatorSelectionStub private selection;
    address private aggregator;
    address private worker;

    function setUp() public {
        aggregator = makeAddr("aggregator");
        worker = makeAddr("worker");

        registry = new ContributionDeviceRegistryStub();
        registry.setAuthorized(worker, true);
        registry.setAuthorized(aggregator, true);

        selection = new ContributionAggregatorSelectionStub();
        selection.setAggregator(aggregator);

        gmStorage =
            new GMStorage(address(registry), address(selection), "initial-model", "initial-signature", aggregator);
    }

    function record(uint256 expectedRound, address target, bytes32 modelHash) private {
        vm.prank(aggregator);
        gmStorage.recordModelSubmission(expectedRound, target, modelHash);
    }

    function completeCurrentRound() private {
        vm.startPrank(aggregator);
        gmStorage.closeModelSubmissions(gmStorage.getRound());
        gmStorage.setGlobalModelAndSignatureAndKeyBundle("global-model", "global-signature", "global-key-bundle");
        gmStorage.incrementRound();
        vm.stopPrank();
        selection.setSelectionRound(1);
    }

    function closeCurrentRound() private {
        uint256 currentRound = gmStorage.getRound();
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(currentRound);
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

    function testDifferentHashForSameWorkerAndRoundReverts() public {
        bytes32 firstHash = keccak256("first-model");
        record(0, worker, firstHash);

        vm.expectRevert(bytes("Conflicting model submission hash"));
        record(0, worker, keccak256("replacement-model"));

        assertEq(gmStorage.modelSubmissionHash(0, worker), firstHash);
        assertEq(gmStorage.getContribution(worker), 1);
    }

    function testWrongExpectedRoundReverts() public {
        vm.expectRevert(bytes("Submission round mismatch"));
        record(1, worker, keccak256("future-round-model"));

        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testUnauthorizedWorkerCannotReceivePoint() public {
        address outsider = makeAddr("outsider");

        vm.expectRevert(bytes("Worker is not authorized"));
        record(0, outsider, keccak256("outsider-model"));

        assertFalse(gmStorage.hasSubmittedModel(0, outsider));
        assertEq(gmStorage.getContribution(outsider), 0);
    }

    function testZeroModelHashReverts() public {
        vm.expectRevert(bytes("Model hash is zero"));
        record(0, worker, bytes32(0));

        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testAggregatorCannotAssignWorkerScoreToItself() public {
        vm.expectRevert(bytes("Current aggregator cannot receive worker score"));
        record(0, aggregator, keccak256("self-score"));

        assertFalse(gmStorage.hasSubmittedModel(0, aggregator));
        assertEq(gmStorage.getContribution(aggregator), 0);
    }

    function testWorkerCannotRecordItsOwnSubmission() public {
        vm.expectRevert(bytes("Caller is not the current aggregator"));
        vm.prank(worker);
        gmStorage.recordModelSubmission(0, worker, keccak256("self-submitted-model"));

        assertFalse(gmStorage.hasSubmittedModel(0, worker));
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testSubmissionAfterGlobalModelPublicationReverts() public {
        vm.startPrank(aggregator);
        gmStorage.closeModelSubmissions(0);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle("global-model", "global-signature", "global-key-bundle");
        vm.stopPrank();

        vm.expectRevert(bytes("Model submissions are closed"));
        record(0, worker, keccak256("late-model"));

        assertEq(gmStorage.modelSubmissionCount(0), 0);
        assertEq(gmStorage.getContribution(worker), 0);
    }

    function testSubmissionWindowClosureRejectsNewModelsButAllowsIdenticalRetry() public {
        bytes32 modelHash = keccak256("accepted-model");
        record(0, worker, modelHash);
        closeCurrentRound();

        record(0, worker, modelHash);

        address secondWorker = makeAddr("second-worker");
        registry.setAuthorized(secondWorker, true);
        vm.expectRevert(bytes("Model submissions are closed"));
        record(0, secondWorker, keccak256("late-model"));

        assertTrue(gmStorage.modelSubmissionsClosed(0));
        assertEq(gmStorage.modelSubmissionCount(0), 1);
        assertEq(gmStorage.getContribution(worker), 1);
        assertEq(gmStorage.getContribution(secondWorker), 0);
    }

    function testOnlyActiveAuthorizedAggregatorCanCloseSubmissionWindow() public {
        vm.expectRevert(bytes("Caller is not the current aggregator"));
        vm.prank(worker);
        gmStorage.closeModelSubmissions(0);

        registry.setAuthorized(aggregator, false);
        vm.expectRevert(bytes("Aggregator is not authorized"));
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(0);

        assertFalse(gmStorage.modelSubmissionsClosed(0));
    }

    function testDeauthorizedAggregatorCannotRecordSubmission() public {
        registry.setAuthorized(aggregator, false);

        vm.expectRevert(bytes("Aggregator is not authorized"));
        record(0, worker, keccak256("worker-model"));

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
