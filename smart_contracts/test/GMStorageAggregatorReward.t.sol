// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";
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
    AggregationPolicy private aggregationPolicy;
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
        gmStorage = new GMStorage(address(registry), address(selection), "initial-model");
        aggregationPolicy = new AggregationPolicy(address(gmStorage));
        aggregationPolicy.configureDefaultPolicy(1, 3600);
        gmStorage.setAggregationPolicyAddress(address(aggregationPolicy));
        vm.prank(aggregator);
        gmStorage.openModelSubmissions(0);
    }

    function _statementSignature(
        address publisher,
        uint256 sourceRound,
        string memory model,
        string memory signatureCid,
        string memory keyBundle,
        bytes32 outputModelHash,
        bytes32 outputBundleHash
    ) private view returns (bytes memory) {
        (
            ,
            bool closed,
            ,
            ,
            ,
            uint32 accepted,
            ,
            bytes32 algorithmHash,
            ,
            ,
            bytes32 policyHash,
            bytes32 inputRoot
        ) = aggregationPolicy.getRoundPolicy(sourceRound);
        require(closed, "test round must be closed");
        bytes32 publicationHash = keccak256(abi.encode(model, signatureCid, keyBundle));
        uint256 nonce = aggregationPolicy.aggregationNonces(publisher);
        bytes32 digest = aggregationPolicy.aggregationStatementDigest(
            sourceRound,
            publisher,
            inputRoot,
            accepted,
            algorithmHash,
            policyHash,
            outputModelHash,
            outputBundleHash,
            publicationHash,
            nonce
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(actionPrivateKeys[publisher], digest);
        return abi.encodePacked(r, s, v);
    }

    function _finalizeCurrentRound(address publisher, string memory suffix) private {
        uint256 sourceRound = gmStorage.getRound();
        vm.startPrank(publisher);
        if (!gmStorage.modelSubmissionsClosed(sourceRound)) {
            gmStorage.closeModelSubmissions(sourceRound);
        }
        vm.stopPrank();

        string memory model = string.concat("model-", suffix);
        string memory signatureCid = string.concat("signature-", suffix);
        string memory keyBundle = string.concat("key-bundle-", suffix);
        bytes32 outputModelHash = keccak256(abi.encodePacked("plaintext:", suffix));
        bytes32 outputBundleHash = keccak256(abi.encodePacked("bundle:", suffix));
        bytes memory actionSignature = _statementSignature(
            publisher,
            sourceRound,
            model,
            signatureCid,
            keyBundle,
            outputModelHash,
            outputBundleHash
        );
        vm.prank(publisher);
        gmStorage.finalizeRoundWithAggregation(
            model,
            signatureCid,
            keyBundle,
            outputModelHash,
            outputBundleHash,
            actionSignature
        );
    }

    function _openAndSubmitRoundOne(address publisher) private {
        selection.setAggregator(publisher);
        selection.setSelectionRound(1);
        vm.prank(publisher);
        gmStorage.openModelSubmissions(1);
        _recordSignedModelSubmission(
            gmStorage,
            publisher,
            publisher,
            worker,
            1,
            keccak256("worker-round-one")
        );
    }

    function testAtomicFinalizationPublishesAdvancesAndRewardsExactlyOnce() public {
        _finalizeCurrentRound(aggregator, "round-zero");

        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getContribution(aggregator), 1);
        assertEq(gmStorage.getLastRoundsAggregator(), aggregator);
        assertTrue(gmStorage.globalModelPublished(0));
        assertEq(gmStorage.globalModelPublisher(0), aggregator);
        assertTrue(gmStorage.roundCompleted(0));
        assertEq(gmStorage.getCompletedRoundCount(), 1);

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        gmStorage.finalizeRoundWithAggregation(
            "other", "other-signature", "other-keys", keccak256("other"), keccak256("other-bundle"), hex"00"
        );
        assertEq(gmStorage.getContribution(aggregator), 1);
    }

    function testDeploymentKeepsUnsignedPlaintextInitialModelAsCidAnchor() public {
        assertEq(gmStorage.initialModelCid(), "initial-model");
        assertEq(gmStorage.getGlobalModel(), "initial-model");
        assertEq(gmStorage.getBackupGlobalModel(), "initial-model");
        assertEq(gmStorage.getGlobalModelSignature(), "");
        assertEq(gmStorage.getBackupGlobalModelSignature(), "");
        assertEq(gmStorage.getGlobalModelKeyBundle(), "");
        assertEq(gmStorage.getBackupGlobalModelKeyBundle(), "");
        assertEq(gmStorage.getLastRoundsAggregator(), address(0));
        assertEq(gmStorage.getCompletedRoundCount(), 0);
        assertFalse(gmStorage.globalModelPublished(0));

        (bool legacyBootstrapSucceeded,) = address(gmStorage).call(
            abi.encodeWithSignature(
                "initializeEncryptedBootstrap(string,string,string,bytes)",
                "replacement",
                "replacement-signature",
                "replacement-key-bundle",
                bytes("replacement-publisher-key")
            )
        );
        assertFalse(legacyBootstrapSucceeded);

        vm.expectRevert(bytes("Initial model CID is empty"));
        new GMStorage(address(registry), address(selection), "");
    }

    function testAtomicFinalizationPromotesPublisherKeyAndFinalizedBundle() public {
        _finalizeCurrentRound(aggregator, "round-zero");

        (
            string memory model,
            string memory signatureCid,
            string memory keyBundle,
            address publisher,
            uint256 modelRound,
            bytes memory publisherKey
        ) = gmStorage.getFinalizedModelBundle();
        assertEq(model, "model-round-zero");
        assertEq(signatureCid, "signature-round-zero");
        assertEq(keyBundle, "key-bundle-round-zero");
        assertEq(publisher, aggregator);
        assertEq(modelRound, 1);
        assertEq(publisherKey, bytes("publisher-public-key"));
        assertEq(gmStorage.activeModelPublisherPublicKey(), bytes("publisher-public-key"));
    }

    function testPublisherKeyIsSnapshottedInsideAtomicFinalization() public {
        _finalizeCurrentRound(aggregator, "round-zero");
        registry.setPublicKey(aggregator, bytes("rotated-after-publication"));

        assertEq(gmStorage.globalModelPublisherPublicKey(0), bytes("publisher-public-key"));
        (,,,,, bytes memory finalizedPublisherKey) = gmStorage.getFinalizedModelBundle();
        assertEq(finalizedPublisherKey, bytes("publisher-public-key"));
    }

    function testRoundZeroCannotBeAbortedOrSkipped() public {
        vm.expectRevert(bytes("Bootstrap round cannot be aborted"));
        vm.prank(address(selection));
        gmStorage.abortRound(aggregator);

        assertEq(gmStorage.getRound(), 0);
        assertFalse(gmStorage.roundAborted(0));
        assertEq(gmStorage.getGlobalModel(), "initial-model");
        assertEq(gmStorage.getGlobalModelSignature(), "");
        assertEq(gmStorage.getGlobalModelKeyBundle(), "");
    }

    function testNonBootstrapRoundRequiresConfiguredMinimum() public {
        _finalizeCurrentRound(aggregator, "round-zero");
        selection.setSelectionRound(1);
        vm.prank(aggregator);
        gmStorage.openModelSubmissions(1);

        vm.expectRevert(bytes("required submissions not reached"));
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(1);
        assertFalse(gmStorage.modelSubmissionsClosed(1));
    }

    function testLegacyPublishAndIncrementSelectorsCannotBypassStatement() public {
        vm.expectRevert(bytes("Aggregation statement and atomic finalization required"));
        vm.prank(aggregator);
        gmStorage.setGlobalModelAndSignatureAndKeyBundle("model", "signature", "keys");

        vm.expectRevert(bytes("Round advancement requires atomic aggregation finalization"));
        vm.prank(aggregator);
        gmStorage.incrementRound();
    }

    function testAggregationPolicyPointerRejectsAccountsAndWrongLedgerBinding() public {
        GMStorage otherStorage = new GMStorage(address(registry), address(selection), "initial-model");

        vm.expectRevert(bytes("aggregation policy has no code"));
        otherStorage.setAggregationPolicyAddress(makeAddr("not-a-contract"));

        vm.expectRevert(bytes("aggregation policy is bound to another GMStorage"));
        otherStorage.setAggregationPolicyAddress(address(aggregationPolicy));

        assertEq(otherStorage.aggregation_policy_address(), address(0));
    }

    function testNextRoundRequiresFreshSelectionAndFreshPolicySnapshot() public {
        _finalizeCurrentRound(aggregator, "round-zero");

        vm.expectRevert(bytes("Aggregator not selected for current round"));
        vm.prank(aggregator);
        gmStorage.openModelSubmissions(1);

        _openAndSubmitRoundOne(aggregator);
        _finalizeCurrentRound(aggregator, "round-one");
        assertEq(gmStorage.getRound(), 2);
        assertEq(gmStorage.getContribution(aggregator), 2);
    }

    function testWrongAggregationStatementCannotPublishOrReward() public {
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(0);

        bytes32 wrongModelHash = keccak256("wrong-model");
        bytes32 bundleHash = keccak256("bundle");
        bytes memory signature = _statementSignature(
            aggregator,
            0,
            "model",
            "signature",
            "keys",
            keccak256("different-model"),
            bundleHash
        );
        vm.expectRevert(bytes("invalid aggregation statement"));
        vm.prank(aggregator);
        gmStorage.finalizeRoundWithAggregation(
            "model", "signature", "keys", wrongModelHash, bundleHash, signature
        );

        assertEq(gmStorage.getRound(), 0);
        assertEq(gmStorage.getContribution(aggregator), 0);
        assertFalse(gmStorage.globalModelPublished(0));
    }

    function testDeauthorizedAggregatorCannotFinalize() public {
        vm.prank(aggregator);
        gmStorage.closeModelSubmissions(0);
        bytes32 modelHash = keccak256("model");
        bytes32 bundleHash = keccak256("bundle");
        bytes memory signature =
            _statementSignature(aggregator, 0, "model", "signature", "keys", modelHash, bundleHash);
        registry.setAuthorized(aggregator, false);

        vm.expectRevert(bytes("participant is not authorized"));
        vm.prank(aggregator);
        gmStorage.finalizeRoundWithAggregation(
            "model", "signature", "keys", modelHash, bundleHash, signature
        );
        assertEq(gmStorage.getRound(), 0);
    }
}
