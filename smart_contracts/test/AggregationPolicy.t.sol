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
    string private constant HYBRID_R_V1_PREIMAGE =
        "VITA-FL:hybrid-r:v1|model=torch-state-dict|layout=conv1.weight,conv1.bias,conv2.weight,conv2.bias,fc1.weight,fc1.bias,fc2.weight,fc2.bias|tensor-order=c-contiguous-row-major|numeric=ieee754-binary64-cpu|update=client-model-minus-parent-model|input-order=worker-address-ascending|candidates=fedavg(equal-weight-arithmetic-mean),coordinate-median(even-count=arithmetic-mean-of-middle-two),trimmed-mean(q=1..floor((n-1)/2),q-ascending,drop-q-lowest-and-q-highest-per-coordinate),multi-krum(n>=5,f=floor((n-3)/2),neighbors=n-f-2,select=n-f-2,single-pass,squared-l2,score=sum-nearest,score-ties=input-order,selected-update=equal-weight-arithmetic-mean)|candidate-order=fedavg,coordinate-median,trimmed-mean-q-ascending,multi-krum|candidate-model=parent-model-plus-candidate-update|validation=round.validationDataHash|risk=bce-with-logits(raw-logits,elementwise-mean-over-Nx14,binary64-cpu)|selection=exact-binary64-less-than;ties=earlier-candidate|gate=best-loss<=parent-loss*(1+round.maxLossIncreaseBps/10000)|parent-fallback=unchanged-parent-if-no-finite-candidate-or-gate-fails|fail-closed=missing-or-hash-mismatched-validation,invalid-model-layout,nonfinite-parent-loss";

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
        (,,,,, uint32 inputCount,, bytes32 algorithmHash,,, bytes32 policyHash, bytes32 inputRoot) =
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
            bytes32 validationDataHash,
            uint16 maxLossIncreaseBps,
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
        assertEq(algorithmHash, policy.HYBRID_R_V1_HASH());
        assertEq(algorithmHash, 0xfee8d99e620214799109487915a0c3a4f37f5a6fb66cb08dfed75fb5b6573610);
        assertEq(algorithmHash, keccak256(bytes(HYBRID_R_V1_PREIMAGE)));
        assertEq(validationDataHash, policy.HYBRID_R_VALIDATION_DATA_V1_HASH());
        assertEq(validationDataHash, 0xe4457c09ceeb203858e9b74232a4aa5b8852c623d85a63742a8751405d189d63);
        assertEq(maxLossIncreaseBps, 500);
        assertEq(
            policyHash,
            keccak256(
                abi.encode(
                    policy.POLICY_HASH_DOMAIN(),
                    configurationVersion,
                    requiredSubmissions,
                    openedAt,
                    deadline,
                    algorithmHash,
                    validationDataHash,
                    maxLossIncreaseBps
                )
            )
        );
        assertEq(inputRoot, policy.INPUT_ROOT_SEED());
    }

    function testBootstrapRoundSnapshotsRolloverWithoutHybridValidationGate() public {
        vm.warp(1000);
        policy.openRound(0);

        (
            ,,
            uint64 openedAt,
            uint64 deadline,
            uint32 requiredSubmissions,,
            uint64 configurationVersion,
            bytes32 algorithmHash,
            bytes32 validationDataHash,
            uint16 maxLossIncreaseBps,
            bytes32 policyHash,
            bytes32 inputRoot
        ) = policy.getRoundPolicy(0);

        assertEq(requiredSubmissions, 0);
        assertEq(algorithmHash, policy.BOOTSTRAP_ROLLOVER_V1_HASH());
        assertEq(validationDataHash, bytes32(0));
        assertEq(maxLossIncreaseBps, 0);
        assertEq(inputRoot, policy.INPUT_ROOT_SEED());
        assertEq(
            policyHash,
            keccak256(
                abi.encode(
                    policy.POLICY_HASH_DOMAIN(),
                    configurationVersion,
                    requiredSubmissions,
                    openedAt,
                    deadline,
                    algorithmHash,
                    validationDataHash,
                    maxLossIncreaseBps
                )
            )
        );
    }

    function testOpenRoundKeepsItsPolicySnapshotAfterDefaultReconfiguration() public {
        vm.warp(1000);
        policy.openRound(1);
        (,,, uint64 firstDeadline, uint32 firstRequired,, uint64 firstVersion,,,, bytes32 firstPolicyHash,) =
            policy.getRoundPolicy(1);

        policy.configureDefaultPolicy(3, 7200);

        (,,, uint64 unchangedDeadline, uint32 unchangedRequired,, uint64 unchangedVersion,,,, bytes32 unchangedHash,) =
            policy.getRoundPolicy(1);
        assertEq(unchangedDeadline, firstDeadline);
        assertEq(unchangedRequired, firstRequired);
        assertEq(unchangedVersion, firstVersion);
        assertEq(unchangedHash, firstPolicyHash);

        policy.openRound(2);
        (,,, uint64 secondDeadline, uint32 secondRequired,, uint64 secondVersion,,,,,) = policy.getRoundPolicy(2);
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

        (bool opened, bool closed,,,, uint32 acceptedSubmissions,,,,,,) = policy.getRoundPolicy(1);
        assertTrue(opened);
        assertFalse(closed);
        assertEq(acceptedSubmissions, 1);
    }

    function testSubmissionAfterDeadlineReverts() public {
        vm.warp(1000);
        policy.openRound(1);
        (,,, uint64 deadline,,,,,,,,) = policy.getRoundPolicy(1);
        vm.warp(uint256(deadline) + 1);

        vm.expectRevert(bytes("round submission deadline passed"));
        policy.recordSubmission(1, workerOne, keccak256("late-submission"));

        (,,,,, uint32 acceptedSubmissions,,,,,,) = policy.getRoundPolicy(1);
        assertEq(acceptedSubmissions, 0);
    }

    function testInputRootCommitsToOrderedWorkersAndCommitments() public {
        bytes32 commitmentOne = keccak256("submission-one");
        bytes32 commitmentTwo = keccak256("submission-two");
        policy.openRound(1);

        policy.recordSubmission(1, workerOne, commitmentOne);
        bytes32 expectedRoot = keccak256(abi.encode(policy.INPUT_ROOT_SEED(), workerOne, commitmentOne));
        (,,,,,,,,,,, bytes32 rootAfterFirst) = policy.getRoundPolicy(1);
        assertEq(rootAfterFirst, expectedRoot);

        policy.recordSubmission(1, workerTwo, commitmentTwo);
        expectedRoot = keccak256(abi.encode(expectedRoot, workerTwo, commitmentTwo));
        policy.closeRound(1, 2);

        (, bool closed,,,, uint32 acceptedSubmissions,,,,,, bytes32 finalRoot) = policy.getRoundPolicy(1);
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
