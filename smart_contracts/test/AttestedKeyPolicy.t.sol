// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;
import {Test} from "forge-std/Test.sol";
import {AttestedKeyPolicy} from "../src/keys/AttestedKeyPolicy.sol";
contract AttestedKeyPolicyTest is Test {
    AttestedKeyPolicy policy;
    address participant = address(11);
    address app = address(22);
    bytes32 image = sha256("exact reviewed compose");
    function setUp() public {vm.chainId(31337); policy = new AttestedKeyPolicy(address(33));}
    function testExactBindingSurvivesRepeatedChecks() public {
        policy.authorise(participant,app,image);
        assertTrue(policy.isAllowed(participant,app,image));assertTrue(policy.isAllowed(participant,app,image));
        assertFalse(policy.isAllowed(participant,app,sha256("replacement image")));
        assertFalse(policy.isAllowed(address(12),app,image));assertFalse(policy.isAllowed(participant,address(23),image));
    }
    function testOperatorCannotBroadenPolicy() public {
        vm.prank(participant); vm.expectRevert("administrator required");policy.authorise(participant,app,image);
        policy.authorise(participant,app,image);
        bytes32 replacement = sha256("replacement");
        vm.expectRevert("binding is immutable");policy.authorise(participant,app,replacement);
    }
    function testRevocationIsPermanent() public {
        policy.authorise(participant,app,image);policy.revoke(participant,app);
        assertFalse(policy.isAllowed(participant,app,image));
        vm.expectRevert("binding is immutable");policy.authorise(participant,app,image);
    }
    function testRejectsPublicChainDeployment() public {
        vm.chainId(1);vm.expectRevert("Anvil chain required");new AttestedKeyPolicy(address(33));
    }
    function testRejectsEmptyGrant() public {
        assertFalse(policy.isAllowed(participant,app,bytes32(0)));
        vm.expectRevert("invalid grant");policy.authorise(participant,app,bytes32(0));
    }
}
