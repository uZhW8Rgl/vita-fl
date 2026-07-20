// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {GMStorage} from "../src/core/GMStorage.sol";

contract AggregatorSelectionStub {
    address private aggregator;

    function setAggregator(address newAggregator) external {
        aggregator = newAggregator;
    }

    function isAggregator(address candidate) external view returns (bool) {
        return candidate == aggregator;
    }
}

contract GMStorageAggregatorRewardTest is Test {
    GMStorage private gmStorage;
    AggregatorSelectionStub private selection;
    address private aggregator;

    function setUp() public {
        aggregator = makeAddr("aggregator");
        selection = new AggregatorSelectionStub();
        selection.setAggregator(aggregator);
        gmStorage = new GMStorage(
            address(0),
            address(selection),
            "initial-model",
            "initial-signature",
            aggregator
        );
    }

    function testIncrementRoundRewardsAggregatorOnce() public {
        vm.prank(aggregator);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 1);
        assertEq(gmStorage.getContribution(aggregator), 1);
        assertEq(gmStorage.getLastRoundsAggregator(), aggregator);
    }

    function testEachCompletedRoundAddsExactlyOnePoint() public {
        vm.startPrank(aggregator);
        gmStorage.incrementRound();
        gmStorage.incrementRound();
        vm.stopPrank();

        assertEq(gmStorage.getRound(), 2);
        assertEq(gmStorage.getContribution(aggregator), 2);
    }

    function testNonAggregatorCannotReceiveRoundReward() public {
        address worker = makeAddr("worker");

        vm.expectRevert(bytes("Caller is not an aggregator"));
        vm.prank(worker);
        gmStorage.incrementRound();

        assertEq(gmStorage.getRound(), 0);
        assertEq(gmStorage.getContribution(worker), 0);
    }
}
