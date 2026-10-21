// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;
import {Test} from "forge-std/Test.sol";
import {DeviceRegistry} from "../src/core/DeviceRegistry.sol";
import {AttestedKeyAdmission} from "../src/keys/AttestedKeyAdmission.sol";
import {MockTdxV4Attestation} from "../src/attestation/MockTdxV4Attestation.sol";
import {Rtmr3Event} from "../src/attestation/Rtmr3Event.sol";

/// @dev Synthetic fixture for HTTP/Anvil integration only. NEVER deploy as a production verifier.
contract SingleAdmissionFixtureVerifier {
    bool public disabled;
    function disable() external { disabled=true; }
    function verifyAndAttestOnChainWithRtmr3EventLog(bytes calldata quote,Rtmr3Event[] calldata,bytes32)
        external view returns(bytes memory){
        require(!disabled,"second appraisal forbidden");
        require(quote.length==637 && quote[0]==0x42,"synthetic fixture quote rejected");
        return quote[568:632];
    }
}

/// @dev Protocol tests use a labelled mock verifier; real DCAP is evaluated separately.
contract AttestedKeyAdmissionTest is Test {
    DeviceRegistry registry; AttestedKeyAdmission admission; MockTdxV4Attestation verifier;
    bytes appCompose; address participant; uint256 participantKey=123; address appId=address(456);
    AttestedKeyAdmission.Identity identity;
    function setUp() public {
        vm.chainId(31337);participant=vm.addr(participantKey);
        registry=new DeviceRegistry(keccak256("single-appraisal test"));
        verifier=new MockTdxV4Attestation();registry.setTdxV4Attestation(address(verifier));
        appCompose=bytes('{"manifest_version":2,"runner":"docker-compose","docker_compose_file":"services:\\n  dfl-worker:\\n    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111\\n"}');
        registry.setExpectedWorkerImageDigest(bytes32(uint256(0x1111111111111111111111111111111111111111111111111111111111111111)));
        registry.setWorkerPolicyHashAllowed(registry.workerPolicyHash(appCompose),true);
        admission=new AttestedKeyAdmission(address(registry),keccak256("service"));
        registry.setKeyAdmissionPolicy(address(admission));
        identity=AttestedKeyAdmission.Identity(address(789),new bytes(400),keccak256("sello"),keccak256("air"));
    }
    function request(bool restoring,uint256 nonce) internal view returns(AttestedKeyAdmission.Request memory){
        return AttestedKeyAdmission.Request(participant,appId,sha256(appCompose),bytes32(nonce),keccak256("recipient"),uint64(block.timestamp+120),restoring,keccak256("service instance"),keccak256("service public key"));
    }
    function events() internal view returns(Rtmr3Event[] memory out){
        out=new Rtmr3Event[](2);out[0]=Rtmr3Event(0x08000001,"app-id",abi.encodePacked(appId));
        out[1]=Rtmr3Event(0x08000001,"compose-hash",abi.encodePacked(sha256(appCompose)));
    }
    function signature(AttestedKeyAdmission.Request memory req) internal view returns(bytes memory){
        bytes32 hash=keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32",admission.authorisationDigest(req,identity)));
        (uint8 v,bytes32 r,bytes32 s)=vm.sign(participantKey,hash);return abi.encodePacked(r,s,v);
    }
    function admit(AttestedKeyAdmission.Request memory req) internal {
        admission.admit(req,identity,signature(req),admission.reportData(req,identity),events(),appCompose);
    }
    function testInitialAdmissionAndRegistrationNeedOnlyOneAppraisal() public {
        AttestedKeyAdmission.Request memory req=request(false,1);admit(req);
        assertTrue(admission.releaseAuthorised(req,identity));
        // Disable the verifier after admission: registration must consume the proof, not verify again.
        vm.mockCallRevert(address(verifier),abi.encodeWithSelector(verifier.verifyAndAttestOnChainWithRtmr3EventLog.selector),"second appraisal forbidden");
        bytes32 digest=registry.enrollmentDigest(participant,identity.actionKey,identity.selloKey,appCompose);
        (uint8 v,bytes32 r,bytes32 s)=vm.sign(participantKey,digest);
        vm.prank(identity.actionKey);
        registry.registerDeviceWithKeyAdmission(appId,appCompose,participant,identity.actionKey,"https://worker.example","",identity.rsaPublicKey,identity.selloKey,abi.encodePacked(r,s,v));
        assertTrue(registry.isAuthorized(participant));assertEq(registry.actionKeyForParticipant(participant),identity.actionKey);
    }
    function testRestoreNeedsNoExistingActionPrivateKey() public {
        admit(request(false,1));AttestedKeyAdmission.Request memory req=request(true,2);admit(req);
        assertTrue(admission.releaseAuthorised(req,identity));
    }
    function testReplayAndExpiryDenyRelease() public {
        AttestedKeyAdmission.Request memory req=request(false,1);admit(req);
        bytes memory sig=signature(req);bytes memory report=admission.reportData(req,identity);Rtmr3Event[] memory log=events();
        vm.expectRevert("challenge already used");admission.admit(req,identity,sig,report,log,appCompose);
        vm.warp(block.timestamp+121);assertFalse(admission.releaseAuthorised(req,identity));
    }
    function testRevocationAndRecipientChangeDenyRelease() public {
        AttestedKeyAdmission.Request memory req=request(false,1);admit(req);
        req.recipientPublicKey=keccak256("attacker");assertFalse(admission.releaseAuthorised(req,identity));
        req.recipientPublicKey=keccak256("recipient");admission.revoke(participant,appId);
        assertFalse(admission.releaseAuthorised(req,identity));
    }
    function testCannotReplaceIdentityOrPretendRestoreOnFirstBoot() public {
        AttestedKeyAdmission.Request memory req=request(true,1);bytes memory sig=signature(req);
        bytes memory report=admission.reportData(req,identity);Rtmr3Event[] memory log=events();
        vm.expectRevert("identity restore mismatch");admission.admit(req,identity,sig,report,log,appCompose);
        admit(request(false,2));identity.actionKey=address(999);req=request(true,3);sig=signature(req);report=admission.reportData(req,identity);
        vm.expectRevert("identity restore mismatch");admission.admit(req,identity,sig,report,log,appCompose);
    }
    function testQuoteCannotBeReusedForDifferentChallenge() public {
        AttestedKeyAdmission.Request memory old=request(false,1);bytes memory report=admission.reportData(old,identity);
        AttestedKeyAdmission.Request memory req=request(false,2);bytes memory sig=signature(req);Rtmr3Event[] memory log=events();
        vm.expectRevert("quote report data mismatch");admission.admit(req,identity,sig,report,log,appCompose);
    }
    function testUnlistedParticipantNeedsNoAdministratorGrant() public {
        participantKey=987654;participant=vm.addr(participantKey);appId=address(987);
        assertTrue(admission.isAllowed(participant,appId,sha256(appCompose)));
        admit(request(false,1));
        (bytes32 hash,bool revoked)=admission.grants(participant,appId);
        assertEq(hash,sha256(appCompose));assertFalse(revoked);
    }
    function testWorkerQuoteCannotBeMovedToAnotherServiceSigner() public {
        AttestedKeyAdmission.Request memory req=request(false,1);bytes memory report=admission.reportData(req,identity);
        req.servicePublicKey=keccak256("substituted service key");
        bytes memory sig=signature(req);Rtmr3Event[] memory log=events();
        vm.expectRevert("quote report data mismatch");admission.admit(req,identity,sig,report,log,appCompose);
    }
}
