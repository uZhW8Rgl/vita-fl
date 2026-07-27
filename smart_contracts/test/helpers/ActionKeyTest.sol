// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {GMStorage} from "../../src/core/GMStorage.sol";

abstract contract ActionKeyTest is Test {
    mapping(address => uint256) internal actionPrivateKeys;

    function _makeActionParticipant(string memory label) internal returns (address participant) {
        uint256 privateKey;
        (participant, privateKey) = makeAddrAndKey(label);
        actionPrivateKeys[participant] = privateKey;
    }

    function _recordSignedModelSubmission(
        GMStorage target,
        address aggregatorAction,
        address aggregatorParticipant,
        address worker,
        uint256 expectedRound,
        bytes32 modelHash
    ) internal {
        bytes32 packageHash = keccak256(abi.encodePacked("package:", modelHash));
        bytes32 parentModelHash = target.currentParentModelHash();
        uint256 workerNonce = target.hasSubmittedModel(expectedRound, worker)
            ? target.modelSubmissionNonce(expectedRound, worker)
            : target.workerSubmissionNonces(worker);
        bytes32 digest = target.modelSubmissionDigest(
            expectedRound, worker, aggregatorParticipant, parentModelHash, modelHash, packageHash, workerNonce
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(actionPrivateKeys[worker], digest);
        vm.prank(aggregatorAction);
        target.recordModelSubmission(
            expectedRound, worker, modelHash, packageHash, parentModelHash, workerNonce, abi.encodePacked(r, s, v)
        );
    }
}
