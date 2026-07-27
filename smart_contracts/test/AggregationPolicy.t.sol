// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AggregationPolicy} from "../src/core/AggregationPolicy.sol";

contract AggregationPolicyTest is Test {
    AggregationPolicy private policy;

    address private aggregator;
    address private actionKey;
    address private wrongActionKey;
    address private workerOne;
    address private workerTwo;
    uint256 private actionPrivateKey;
    uint256 private wrongActionPrivateKey;

    bytes32 private constant OUTPUT_MODEL_HASH = keccak256("output-model");
    bytes32 private constant OUTPUT_BUNDLE_HASH = keccak256("output-bundle");
    bytes32 private constant PUBLICATION_HASH = keccak256("publication");

    function setUp() public {
        aggregator = makeAddr("aggregator");
        (actionKey, actionPrivateKey) = makeAddrAndKey("aggregator-action-key");
        (wrongActionKey, wrongActionPrivateKey) = makeAddrAndKey("wrong-action-key");
        workerOne = makeAddr("worker-one");
        workerTwo = makeAddr("worker-two");

        policy = new AggregationPolicy(address(this));
        policy.configureDefaultPolicy(2, 3600);
    }

    function signStatement(uint256 round, uint256 nonce, uint256 privateKey) private returns (bytes memory) {
        (,,,,, uint32 inputCount,, bytes32 algorithmHash, bytes32 policyHash, bytes32 inputRoot) =
            policy.getRoundPolicy(round);
        bytes32 digest = policy.aggregationStatementDigest(
            round,
            aggregator,
            inputRoot,
            inputCount,
            algorithmHash,
            policyHash,
            OUTPUT_MODEL_HASH,
            OUTPUT_BUNDLE_HASH,
            PUBLICATION_HASH,
            nonce
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function publish(uint256 round, bytes memory signature) private {
        policy.verifyAndRecordPublication(
            round, aggregator, actionKey, OUTPUT_MODEL_HASH, OUTPUT_BUNDLE_HASH, PUBLICATION_HASH, signature
        );
    }

    function testRoundSnapshotsConfiguredDeadlineMinimumAndAlgorithm() public {
        vm.warp(1000);
        policy.openRound(1);

        (
            bool opened,
            bool closed,
            uint64 openedAt,
            uint64 deadline,
            uint32 requiredSubmissions,
            uint32 acceptedSubmissions,
            uint64 configurationVersion,
            bytes32 algorithmHash,
            bytes32 policyHash,
            bytes32 inputRoot
        ) = policy.getRoundPolicy(1);

        assertTrue(opened);
        assertFalse(closed);
        assertEq(openedAt, 1000);
        assertEq(deadline, 4600);
        assertEq(requiredSubmissions, 2);
        assertEq(acceptedSubmissions, 0);
        assertEq(configurationVersion, 1);
        assertEq(algorithmHash, policy.FEDERATED_AVERAGING_V1_HASH());
        assertTrue(policyHash != bytes32(0));
        assertEq(inputRoot, policy.INPUT_ROOT_SEED());
    }

    function testOpenRoundKeepsItsPolicySnapshotAfterDefaultReconfiguration() public {
        vm.warp(1000);
        policy.openRound(1);
        (,,, uint64 firstDeadline, uint32 firstRequired,, uint64 firstVersion,, bytes32 firstPolicyHash,) =
            policy.getRoundPolicy(1);

        policy.configureDefaultPolicy(3, 7200);

        (,,, uint64 unchangedDeadline, uint32 unchangedRequired,, uint64 unchangedVersion,, bytes32 unchangedHash,) =
            policy.getRoundPolicy(1);
        assertEq(unchangedDeadline, firstDeadline);
        assertEq(unchangedRequired, firstRequired);
        assertEq(unchangedVersion, firstVersion);
        assertEq(unchangedHash, firstPolicyHash);

        policy.openRound(2);
        (,,, uint64 secondDeadline, uint32 secondRequired,, uint64 secondVersion,,,)
            = policy.getRoundPolicy(2);
        assertEq(secondDeadline, 8200);
        assertEq(secondRequired, 3);
        assertEq(secondVersion, 2);
    }

    function testOnlyOwnerCanReconfigureDefaults() public {
        vm.expectRevert(bytes("not aggregation policy owner"));
        vm.prank(makeAddr("outsider"));
        policy.configureDefaultPolicy(1, 1);
    }

    function testCloseBeforeRequiredSubmissionCountReverts() public {
        policy.openRound(1);
        policy.recordSubmission(1, workerOne, keccak256("submission-one"));

        vm.expectRevert(bytes("required submissions not reached"));
        policy.closeRound(1, 1);

        (bool opened, bool closed,,,, uint32 acceptedSubmissions,,,,) = policy.getRoundPolicy(1);
        assertTrue(opened);
        assertFalse(closed);
        assertEq(acceptedSubmissions, 1);
    }

    function testSubmissionAfterDeadlineReverts() public {
        vm.warp(1000);
        policy.openRound(1);
        (,,, uint64 deadline,,,,,,) = policy.getRoundPolicy(1);
        vm.warp(uint256(deadline) + 1);

        vm.expectRevert(bytes("round submission deadline passed"));
        policy.recordSubmission(1, workerOne, keccak256("late-submission"));

        (,,,,, uint32 acceptedSubmissions,,,,) = policy.getRoundPolicy(1);
        assertEq(acceptedSubmissions, 0);
    }

    function testInputRootCommitsToOrderedWorkersAndCommitments() public {
        bytes32 commitmentOne = keccak256("submission-one");
        bytes32 commitmentTwo = keccak256("submission-two");
        policy.openRound(1);

        policy.recordSubmission(1, workerOne, commitmentOne);
        bytes32 expectedRoot = keccak256(abi.encode(policy.INPUT_ROOT_SEED(), workerOne, commitmentOne));
        (,,,,,,,,, bytes32 rootAfterFirst) = policy.getRoundPolicy(1);
        assertEq(rootAfterFirst, expectedRoot);

        policy.recordSubmission(1, workerTwo, commitmentTwo);
        expectedRoot = keccak256(abi.encode(expectedRoot, workerTwo, commitmentTwo));
        policy.closeRound(1, 2);

        (, bool closed,,,, uint32 acceptedSubmissions,,,, bytes32 finalRoot) = policy.getRoundPolicy(1);
        assertTrue(closed);
        assertEq(acceptedSubmissions, 2);
        assertEq(finalRoot, expectedRoot);
    }

    function testWrongAggregationActionKeySignatureReverts() public {
        policy.openRound(0);
        policy.closeRound(0, 0);
        bytes memory wrongSignature = signStatement(0, 0, wrongActionPrivateKey);

        vm.expectRevert(bytes("invalid aggregation statement"));
        publish(0, wrongSignature);

        (bool published,,,,,,,,,,,,) = policy.getAggregationEvidence(0);
        assertFalse(published);
        assertTrue(wrongActionKey != actionKey);
    }

    function testStaleAggregationNonceSignatureReverts() public {
        policy.openRound(0);
        policy.closeRound(0, 0);
        publish(0, signStatement(0, 0, actionPrivateKey));
        assertEq(policy.aggregationNonces(aggregator), 1);

        policy.openRound(1);
        policy.recordSubmission(1, workerOne, keccak256("submission-one"));
        policy.recordSubmission(1, workerTwo, keccak256("submission-two"));
        policy.closeRound(1, 2);

        bytes memory staleSignature = signStatement(1, 0, actionPrivateKey);
        vm.expectRevert(bytes("invalid aggregation statement"));
        publish(1, staleSignature);

        (bool published,,,,,,,,,,,,) = policy.getAggregationEvidence(1);
        assertFalse(published);
        assertEq(policy.aggregationNonces(aggregator), 1);
    }
}
