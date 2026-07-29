// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";
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
    AggregationPolicy private aggregationPolicy;
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
        aggregationPolicy = new AggregationPolicy(address(gmStorage));
        aggregationPolicy.configureDefaultPolicy(1, 3600);
        gmStorage.setAggregationPolicyAddress(address(aggregationPolicy));

        vm.prank(completedAggregator);
        gmStorage.openModelSubmissions(0);
        finalizeCurrentRound(
            completedAggregator, completedAggregator, "completed-model", "completed-signature", "completed-key-bundle"
        );

        selection.setAggregator(failedAggregator);
        selection.setSelectionRound(1);
        vm.prank(failedAggregator);
        gmStorage.openModelSubmissions(1);
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

    function aggregationSignature(
        address aggregator,
        uint256 signerPrivateKey,
        string memory model,
        string memory signatureCid,
        string memory keyBundle
    ) private returns (bytes memory) {
        uint256 currentRound = gmStorage.getRound();
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
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerPrivateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function finalizeCurrentRound(
        address aggregator,
        address aggregatorAction,
        string memory model,
        string memory signatureCid,
        string memory keyBundle
    ) private {
        uint256 currentRound = gmStorage.getRound();
        if (!gmStorage.modelSubmissionsClosed(currentRound)) {
            vm.prank(aggregatorAction);
            gmStorage.closeModelSubmissions(currentRound);
        }
        bytes memory statementSignature =
            aggregationSignature(aggregator, actionPrivateKeys[aggregatorAction], model, signatureCid, keyBundle);
        bytes32 outputModelHash = keccak256(bytes(model));
        bytes32 outputBundleHash = keccak256(abi.encode(model, signatureCid, keyBundle, outputModelHash));

        vm.prank(aggregatorAction);
        gmStorage.finalizeRoundWithAggregation(
            model, signatureCid, keyBundle, outputModelHash, outputBundleHash, statementSignature
        );
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

    function testAbortAfterRejectedPublicationKeepsLastCompletedArtifacts() public {
        bytes memory activePublisherKeyBeforeAttempt = gmStorage.activeModelPublisherPublicKey();
        _recordSignedModelSubmission(
            gmStorage, failedAggregator, failedAggregator, completedAggregator, 1, keccak256("worker-model")
        );

        vm.prank(failedAggregator);
        gmStorage.closeModelSubmissions(1);
        bytes memory staleSignature = aggregationSignature(
            failedAggregator,
            actionPrivateKeys[completedAggregator],
            "attempted-model",
            "attempted-signature",
            "attempted-key-bundle"
        );
        vm.expectRevert(bytes("invalid aggregation statement"));
        vm.prank(failedAggregator);
        gmStorage.finalizeRoundWithAggregation(
            "attempted-model",
            "attempted-signature",
            "attempted-key-bundle",
            keccak256(bytes("attempted-model")),
            keccak256(
                abi.encode(
                    "attempted-model",
                    "attempted-signature",
                    "attempted-key-bundle",
                    keccak256(bytes("attempted-model"))
                )
            ),
            staleSignature
        );

        assertCompletedArtifactsActive();
        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);

        abortCurrentRound();

        assertEq(gmStorage.getRound(), 2);
        assertCompletedArtifactsActive();
        assertEq(gmStorage.getBackupGlobalModel(), "initial-model");
        assertEq(gmStorage.getBackupGlobalModelSignature(), "initial-signature");
        assertEq(gmStorage.getBackupGlobalModelKeyBundle(), "");
        assertEq(gmStorage.getLastRoundsAggregator(), completedAggregator);
        assertTrue(gmStorage.roundAborted(1));
        assertEq(gmStorage.getCompletedRoundCount(), 1);
        assertFalse(gmStorage.roundCompleted(1));
        assertFalse(gmStorage.globalModelPublished(1));
        assertEq(gmStorage.globalModelPublisher(1), address(0));
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
