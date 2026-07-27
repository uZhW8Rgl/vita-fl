// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {GMStorage} from "../src/core/GMStorage.sol";
import {ActionKeyTest} from "./helpers/ActionKeyTest.sol";

contract AbortDeviceRegistryStub {
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

contract AbortAggregatorSelectionStub {
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

contract GMStorageAbortTest is ActionKeyTest {
    GMStorage private gmStorage;
    AbortDeviceRegistryStub private registry;
    AbortAggregatorSelectionStub private selection;

    address private completedAggregator;
    address private failedAggregator;

    function setUp() public {
        completedAggregator = _makeActionParticipant("completed-aggregator");
        failedAggregator = _makeActionParticipant("failed-aggregator");

        registry = new AbortDeviceRegistryStub();
        registry.setAuthorized(completedAggregator, true);
        registry.setAuthorized(failedAggregator, true);
        selection = new AbortAggregatorSelectionStub();
        selection.setAggregator(completedAggregator);
        gmStorage = new GMStorage(
            address(registry), address(selection), "initial-model", "initial-signature", completedAggregator
        );

        vm.startPrank(completedAggregator);
        gmStorage.closeModelSubmissions(0);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            "completed-model", "completed-signature", "completed-key-bundle"
        );
        gmStorage.incrementRound();
        vm.stopPrank();

        selection.setAggregator(failedAggregator);
        selection.setSelectionRound(1);
    }

    function abortCurrentRound() private {
        vm.prank(address(selection));
        gmStorage.abortRound(failedAggregator);
    }

    function assertCompletedArtifactsActive() private view {
        assertEq(gmStorage.getGlobalModel(), "completed-model");
        assertEq(gmStorage.getGlobalModelSignature(), "completed-signature");
        assertEq(gmStorage.getGlobalModelKeyBundle(), "completed-key-bundle");
    }

    function testAbortBeforePublicationLeavesActiveArtifactsUnchanged() public {
        assertCompletedArtifactsActive();

        abortCurrentRound();

        assertEq(gmStorage.getRound(), 2);
        assertCompletedArtifactsActive();
        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);
        assertTrue(gmStorage.roundAborted(1));
        assertEq(gmStorage.getCompletedRoundCount(), 1);
        assertFalse(gmStorage.roundCompleted(1));
        assertFalse(gmStorage.globalModelPublished(1));
        assertEq(gmStorage.globalModelPublisher(1), address(0));
    }

    function testAbortAfterPublicationRestoresLastCompletedArtifacts() public {
        bytes memory activePublisherKeyBeforeAttempt = gmStorage.activeModelPublisherPublicKey();
        _recordSignedModelSubmission(
            gmStorage, failedAggregator, failedAggregator, completedAggregator, 1, keccak256("worker-model")
        );

        vm.startPrank(failedAggregator);
        gmStorage.closeModelSubmissions(1);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            "attempted-model", "attempted-signature", "attempted-key-bundle"
        );
        gmStorage.setGlobalModelAndSignatureAndKeyBundle("retried-model", "retried-signature", "retried-key-bundle");
        vm.stopPrank();

        assertEq(gmStorage.getGlobalModel(), "retried-model");
        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);
        vm.expectRevert(bytes("Current round publication is not finalized"));
        gmStorage.getFinalizedModelBundle();

        abortCurrentRound();

        assertEq(gmStorage.getRound(), 2);
        assertCompletedArtifactsActive();
        assertEq(gmStorage.getBackupGlobalModel(), "completed-model");
        assertEq(gmStorage.getBackupGlobalModelSignature(), "completed-signature");
        assertEq(gmStorage.getBackupGlobalModelKeyBundle(), "completed-key-bundle");
        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);
        assertTrue(gmStorage.roundAborted(1));
        assertEq(gmStorage.getCompletedRoundCount(), 1);
        assertFalse(gmStorage.roundCompleted(1));
        assertTrue(gmStorage.globalModelPublished(1));
        assertEq(gmStorage.globalModelPublisher(1), failedAggregator);
        assertEq(gmStorage.activeModelPublisherPublicKey(), activePublisherKeyBeforeAttempt);
        (
            string memory model,
            string memory signature,
            string memory keyBundle,
            address publisher,
            uint256 modelRound,
            bytes memory publisherPublicKey
        ) = gmStorage.getFinalizedModelBundle();
        assertEq(model, "completed-model");
        assertEq(signature, "completed-signature");
        assertEq(keyBundle, "completed-key-bundle");
        assertEq(publisher, completedAggregator);
        assertEq(modelRound, 1);
        assertEq(publisherPublicKey, bytes("publisher-public-key"));
    }

    function testNonFinalizedAggregatorCannotOverwriteLastCompletedSigner() public {
        vm.expectRevert(bytes("Caller did not complete previous round"));
        vm.prank(failedAggregator);
        gmStorage.setLastRoundAggregator();

        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);
    }

    function testAbortRejectsNonCurrentFailedAggregator() public {
        vm.expectRevert(bytes("Failed aggregator is not current aggregator"));
        vm.prank(address(selection));
        gmStorage.abortRound(completedAggregator);

        assertEq(gmStorage.getRound(), 1);
        assertFalse(gmStorage.roundAborted(1));
        assertCompletedArtifactsActive();
        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);
    }
}
