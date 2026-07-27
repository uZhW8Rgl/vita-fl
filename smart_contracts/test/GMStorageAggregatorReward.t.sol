// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {GMStorage} from "../src/core/GMStorage.sol";
import {ActionKeyTest} from "./helpers/ActionKeyTest.sol";

contract RewardDeviceRegistryStub {
    mapping(address => bool) private authorized;
    mapping(address => bytes) private publicKeys;
    address[] private authorizedDevices;

    function setAuthorized(address device, bool value) external {
        if (value && !authorized[device]) {
            authorizedDevices.push(device);
            publicKeys[device] = bytes("publisher-public-key");
        }
        authorized[device] = value;
    }

    function setPublicKey(address device, bytes calldata publicKey) external {
        publicKeys[device] = publicKey;
    }

    function isAuthorized(address device) external view returns (bool) {
        return authorized[device];
    }

    function getAuthorizedDevices() external view returns (address[] memory) {
        return authorizedDevices;
    }

    function getDevice(address device) external view returns (bool, string memory, string memory, bytes memory) {
        return (authorized[device], "", "", publicKeys[device]);
    }

    function actionKeys(address participant) external pure returns (address) {
        return participant;
    }

    function resolveAuthorizedParticipant(address actionKey) external view returns (address) {
        require(authorized[actionKey], "participant is not authorized");
        return actionKey;
    }
}

contract AggregatorSelectionStub {
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

contract GMStorageAggregatorRewardTest is ActionKeyTest {
    GMStorage private gmStorage;
    RewardDeviceRegistryStub private registry;
    AggregatorSelectionStub private selection;
    address private aggregator;
    address private replacementAggregator;
    address private worker;

    function setUp() public {
        aggregator = _makeActionParticipant("aggregator");
        replacementAggregator = _makeActionParticipant("replacement-aggregator");
        worker = _makeActionParticipant("worker");
        registry = new RewardDeviceRegistryStub();
        registry.setAuthorized(aggregator, true);
        registry.setAuthorized(replacementAggregator, true);
        registry.setAuthorized(worker, true);
        registry.setPublicKey(replacementAggregator, bytes("replacement-publisher-public-key"));
        selection = new AggregatorSelectionStub();
        selection.setAggregator(aggregator);
        gmStorage =
            new GMStorage(address(registry), address(selection), "initial-model", "initial-signature", aggregator);
    }

    function publishCurrentRound(address publisher, string memory suffix) private {
        vm.startPrank(publisher);
        uint256 currentRound = gmStorage.getRound();
        if (!gmStorage.modelSubmissionsClosed(currentRound)) {
            gmStorage.closeModelSubmissions(currentRound);
        }
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            string.concat("model-", suffix), string.concat("signature-", suffix), string.concat("key-bundle-", suffix)
        );
        vm.stopPrank();
    }

    function testPublishedRoundRewardsPublishingAggregatorOnce() public {
        publishCurrentRound(aggregator, "round-zero");

        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getContribution(aggregator), 1);
        assertEq(gmStorage.getLastRoundsAggregator(), aggregator);
        assertTrue(gmStorage.globalModelPublished(0));
        assertEq(gmStorage.globalModelPublisher(0), aggregator);
        assertTrue(gmStorage.roundCompleted(0));
        assertEq(gmStorage.getCompletedRoundCount(), 1);
    }

    function testEncryptedBootstrapIsOneShotAndDoesNotFinalizeRound() public {
        gmStorage.initializeEncryptedBootstrap(
            "bootstrap-model", "bootstrap-signature", "bootstrap-key-bundle", bytes("bootstrap-publisher-key")
        );

        assertEq(gmStorage.getGlobalModel(), "bootstrap-model");
        assertEq(gmStorage.getGlobalModelSignature(), "bootstrap-signature");
        assertEq(gmStorage.getGlobalModelKeyBundle(), "bootstrap-key-bundle");
        assertEq(gmStorage.getBackupGlobalModel(), "bootstrap-model");
        assertEq(gmStorage.activeModelPublisherPublicKey(), bytes("bootstrap-publisher-key"));
        assertEq(gmStorage.getCompletedRoundCount(), 0);
        assertFalse(gmStorage.globalModelPublished(0));
        assertEq(gmStorage.bootstrapAuthority(), address(0));

        vm.expectRevert(bytes("Caller is not bootstrap authority"));
        gmStorage.initializeEncryptedBootstrap(
            "replacement", "replacement-signature", "replacement-key-bundle", bytes("replacement-publisher-key")
        );
    }

    function testOnlyDeploymentAuthorityCanInitializeEncryptedBootstrap() public {
        vm.expectRevert(bytes("Caller is not bootstrap authority"));
        vm.prank(aggregator);
        gmStorage.initializeEncryptedBootstrap(
            "bootstrap-model", "bootstrap-signature", "bootstrap-key-bundle", bytes("bootstrap-publisher-key")
        );

        assertEq(gmStorage.getGlobalModel(), "initial-model");
        assertEq(gmStorage.getGlobalModelKeyBundle(), "");
    }

    function testEncryptedBootstrapRejectsEmptyPublisherKeyAtomically() public {
        vm.expectRevert(bytes("Bootstrap publisher public key is empty"));
        gmStorage.initializeEncryptedBootstrap("bootstrap-model", "bootstrap-signature", "bootstrap-key-bundle", "");

        assertEq(gmStorage.getGlobalModel(), "initial-model");
        assertEq(gmStorage.getGlobalModelKeyBundle(), "");
        assertEq(gmStorage.activeModelPublisherPublicKey(), "");
        assertEq(gmStorage.bootstrapAuthority(), address(this));
    }

    function testActiveBundleUsesBootstrapSignerUntilSuccessfulFinalization() public {
        gmStorage.initializeEncryptedBootstrap(
            "bootstrap-model", "bootstrap-signature", "bootstrap-key-bundle", bytes("bootstrap-publisher-key")
        );

        (
            string memory bootstrapModel,
            string memory bootstrapSignature,
            string memory bootstrapKeyBundle,
            address bootstrapPublisher,
            bytes memory bootstrapPublisherKey
        ) = gmStorage.getActiveModelBundle();
        assertEq(bootstrapModel, "bootstrap-model");
        assertEq(bootstrapSignature, "bootstrap-signature");
        assertEq(bootstrapKeyBundle, "bootstrap-key-bundle");
        assertEq(bootstrapPublisher, aggregator);
        assertEq(bootstrapPublisherKey, bytes("bootstrap-publisher-key"));

        publishCurrentRound(aggregator, "round-zero");

        (
            string memory pendingModel,
            string memory pendingSignature,
            string memory pendingKeyBundle,
            address pendingPublisher,
            bytes memory pendingPublisherKey
        ) = gmStorage.getActiveModelBundle();
        assertEq(pendingModel, "bootstrap-model");
        assertEq(pendingSignature, "bootstrap-signature");
        assertEq(pendingKeyBundle, "bootstrap-key-bundle");
        assertEq(pendingPublisher, aggregator);
        assertEq(pendingPublisherKey, bytes("bootstrap-publisher-key"));

        vm.prank(aggregator);
        gmStorage.incrementRound();

        (
            string memory finalizedModel,
            string memory finalizedSignature,
            string memory finalizedKeyBundle,
            address finalizedPublisher,
            bytes memory finalizedPublisherKey
        ) = gmStorage.getActiveModelBundle();
        assertEq(finalizedModel, "model-round-zero");
        assertEq(finalizedSignature, "signature-round-zero");
        assertEq(finalizedKeyBundle, "key-bundle-round-zero");
        assertEq(finalizedPublisher, aggregator);
        assertEq(finalizedPublisherKey, bytes("publisher-public-key"));
        assertEq(gmStorage.activeModelPublisherPublicKey(), bytes("publisher-public-key"));
    }

    function testFinalizationPromotesPublisherKeyCapturedAtomicallyWithPublication() public {
        publishCurrentRound(aggregator, "round-zero");
        assertEq(gmStorage.globalModelPublisherPublicKey(0), bytes("publisher-public-key"));

        registry.setPublicKey(aggregator, bytes("rotated-after-publication"));
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.activeModelPublisherPublicKey(), bytes("publisher-public-key"));
        (,,,,, bytes memory finalizedPublisherKey) = gmStorage.getFinalizedModelBundle();
        assertEq(finalizedPublisherKey, bytes("publisher-public-key"));
    }

    function testRoundZeroAbortRetainsBootstrapBundleAndPublisherKey() public {
        gmStorage.initializeEncryptedBootstrap(
            "bootstrap-model", "bootstrap-signature", "bootstrap-key-bundle", bytes("bootstrap-publisher-key")
        );

        vm.prank(address(selection));
        gmStorage.abortRound(aggregator);

        (
            string memory model,
            string memory signature,
            string memory keyBundle,
            address publisher,
            bytes memory publisherKey
        ) = gmStorage.getActiveModelBundle();
        assertEq(model, "bootstrap-model");
        assertEq(signature, "bootstrap-signature");
        assertEq(keyBundle, "bootstrap-key-bundle");
        assertEq(publisher, aggregator);
        assertEq(publisherKey, bytes("bootstrap-publisher-key"));
        assertEq(gmStorage.getRound(), 1);
        assertTrue(gmStorage.roundAborted(0));

        selection.setAggregator(replacementAggregator);
        selection.setSelectionRound(1);
        _recordSignedModelSubmission(
            gmStorage, replacementAggregator, replacementAggregator, worker, 1, keccak256("replacement-worker-model")
        );
        publishCurrentRound(replacementAggregator, "replacement-round");
        vm.prank(replacementAggregator);
        gmStorage.incrementRound();

        (string memory replacementModel,,, address replacementPublisher, bytes memory replacementPublisherKey) =
            gmStorage.getActiveModelBundle();
        assertEq(replacementModel, "model-replacement-round");
        assertEq(replacementPublisher, replacementAggregator);
        assertEq(replacementPublisherKey, bytes("replacement-publisher-public-key"));
        assertEq(gmStorage.activeModelPublisherPublicKey(), bytes("replacement-publisher-public-key"));
    }

    function testFinalizedBundleViewRejectsPendingPublicationAndReturnsOnlyCompletedModel() public {
        vm.expectRevert(bytes("No finalized model"));
        gmStorage.getFinalizedModelBundle();

        publishCurrentRound(aggregator, "round-zero");
        vm.prank(aggregator);
        gmStorage.incrementRound();

        (
            string memory model,
            string memory signature,
            string memory keyBundle,
            address publisher,
            uint256 modelRound,
            bytes memory publisherPublicKey
        ) = gmStorage.getFinalizedModelBundle();
        assertEq(model, "model-round-zero");
        assertEq(signature, "signature-round-zero");
        assertEq(keyBundle, "key-bundle-round-zero");
        assertEq(publisher, aggregator);
        assertEq(modelRound, 1);
        assertEq(publisherPublicKey, bytes("publisher-public-key"));

        registry.setAuthorized(aggregator, false);
        (,,,,, bytes memory retainedPublisherPublicKey) = gmStorage.getFinalizedModelBundle();
        assertEq(retainedPublisherPublicKey, bytes("publisher-public-key"));
        registry.setAuthorized(aggregator, true);

        selection.setSelectionRound(1);
        _recordSignedModelSubmission(gmStorage, aggregator, aggregator, worker, 1, keccak256("worker-round-one"));
        publishCurrentRound(aggregator, "round-one");

        vm.expectRevert(bytes("Current round publication is not finalized"));
        gmStorage.getFinalizedModelBundle();
    }

    function testNextRoundRequiresSelectionBeforeAnotherCompletion() public {
        publishCurrentRound(aggregator, "round-zero");
        vm.prank(aggregator);
        gmStorage.incrementRound();

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(1);

        selection.setSelectionRound(1);
        _recordSignedModelSubmission(gmStorage, aggregator, aggregator, worker, 1, keccak256("worker-round-one"));
        publishCurrentRound(aggregator, "round-one");
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 2);
        assertEq(gmStorage.getContribution(aggregator), 2);
    }

    function testNonBootstrapPublicationRequiresConfirmedWorkerSubmission() public {
        publishCurrentRound(aggregator, "round-zero");
        vm.prank(aggregator);
        gmStorage.incrementRound();
        selection.setSelectionRound(1);

        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(1);
        vm.expectRevert(bytes("No confirmed worker submissions"));
        vm.prank(aggregator);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle(
            "model-round-one", "signature-round-one", "key-bundle-round-one"
        );

        assertFalse(gmStorage.globalModelPublished(1));
    }

    function testIncrementRoundWithoutPublicationReverts() public {
        vm.expectRevert(bytes("Global model not published for round"));
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 0);
        assertEq(gmStorage.getContribution(aggregator), 0);
    }

    function testCannotIncrementAgainWithoutNextRoundPublication() public {
        publishCurrentRound(aggregator, "round-zero");

        vm.prank(aggregator);
        gmStorage.incrementRound();

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getContribution(aggregator), 1);
    }

    function testRepublishingBeforeCompletionUpdatesReferencesWithoutReward() public {
        publishCurrentRound(aggregator, "first");
        publishCurrentRound(aggregator, "second");

        assertEq(gmStorage.getGlobalModel(), "model-second");
        assertEq(gmStorage.getBackupGlobalModel(), "initial-model");
        assertEq(gmStorage.getBackupGlobalModelSignature(), "initial-signature");
        assertEq(gmStorage.globalModelPublisher(0), aggregator);
        assertEq(gmStorage.getContribution(aggregator), 0);

        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getContribution(aggregator), 1);

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getContribution(aggregator), 1);
    }

    function testOnlyPublishingCurrentAggregatorCanCompleteRound() public {
        publishCurrentRound(aggregator, "round-zero");
        selection.setAggregator(replacementAggregator);

        vm.expectRevert(bytes("Caller is not the current aggregator"));
        vm.prank(aggregator);
        gmStorage.incrementRound();

        vm.expectRevert(bytes("Caller did not publish current round model"));
        vm.prank(replacementAggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 0);
        assertEq(gmStorage.getContribution(aggregator), 0);
        assertEq(gmStorage.getContribution(replacementAggregator), 0);
    }

    function testDeauthorizedAggregatorCannotFinalizePublishedRound() public {
        publishCurrentRound(aggregator, "round-zero");
        registry.setAuthorized(aggregator, false);

        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 0);
        assertFalse(gmStorage.roundCompleted(0));
        assertEq(gmStorage.getContribution(aggregator), 0);
    }

    function testNonAggregatorCannotReceiveRoundReward() public {
        address worker = makeAddr("worker");
        publishCurrentRound(aggregator, "round-zero");

        vm.expectRevert(bytes("Caller is not the current aggregator"));
        vm.prank(worker);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 0);
        assertEq(gmStorage.getContribution(worker), 0);
    }
}
