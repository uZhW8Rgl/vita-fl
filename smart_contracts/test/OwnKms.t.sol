// SPDX-License-Identifier: MIT
pragma solidity ^0.8.22;

import {DeviceRegistryAttestationFixture} from "./DeviceRegistryAttestationBinding.t.sol";
import {DstackKms} from "../vendor/dstack-v0.5.9/contracts/DstackKms.sol";
import {IAppAuth as UpstreamAppAuth} from "../vendor/dstack-v0.5.9/contracts/IAppAuth.sol";
import {ERC1967Proxy} from "../vendor/openzeppelin-5.4.0/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {WorkerAppAuthFactory} from "../src/kms/WorkerAppAuthFactory.sol";

contract OwnKmsTest is DeviceRegistryAttestationFixture {
    DstackKms internal kms;
    WorkerAppAuthFactory internal factory;
    address internal appId;
    bytes32 internal constant OS = keccak256("approved OS");

    function setUp() public override {
        super.setUp();
        DstackKms implementation = new DstackKms();
        kms = DstackKms(address(new ERC1967Proxy(address(implementation),
            abi.encodeCall(DstackKms.initialize, (address(this), address(0))))));
        factory = new WorkerAppAuthFactory(address(registry), address(kms));
        registry.setKmsAppFactory(address(factory), keccak256("trusted-kms"));
        kms.addOsImageHash(OS);
        vm.prank(worker);
        appId = factory.createApp(appCompose, bytes32(0));
    }

    function _boot() internal view returns (UpstreamAppAuth.AppBootInfo memory b) {
        b.appId = appId;
        b.composeHash = sha256(appCompose);
        b.instanceId = address(123);
        b.deviceId = keccak256("device");
        b.mrAggregated = keccak256("KMS measurement");
        b.osImageHash = OS;
        b.tcbStatus = "UpToDate";
        b.advisoryIds = new string[](0);
    }

    function testOriginalKmsDelegatesToImmutableWorkerPolicy() public view {
        (bool allowed,) = kms.isAppAllowed(_boot());
        assertTrue(allowed);
        assertTrue(kms.registeredApps(appId));
        assertFalse(registry.isAuthorized(worker), "preboot policy is not DFL registration");
    }

    function testSameAppCannotRecoverThroughChangedWorkloadPolicy() public view {
        UpstreamAppAuth.AppBootInfo memory b = _boot();
        b.composeHash = keccak256("replacement image");
        (bool allowed, string memory reason) = kms.isAppAllowed(b);
        assertFalse(allowed);
        assertEq(reason, "compose hash mismatch");
    }

    function testUnknownOsAndStaleTcbDenied() public view {
        UpstreamAppAuth.AppBootInfo memory b = _boot();
        b.osImageHash = keccak256("other OS");
        (bool allowed,) = kms.isAppAllowed(b);
        assertFalse(allowed);
        b.osImageHash = OS;
        b.tcbStatus = "OutOfDate";
        (allowed,) = kms.isAppAllowed(b);
        assertFalse(allowed);
    }

    function testOperatorCannotChangeWorkerAllowlistOrUpgradeIt() public {
        vm.startPrank(worker);
        (bool ok,) = appId.call(abi.encodeWithSignature("addComposeHash(bytes32)", keccak256("replacement")));
        assertFalse(ok);
        (ok,) = appId.call(abi.encodeWithSignature("upgradeToAndCall(address,bytes)", address(kms), bytes("")));
        assertFalse(ok);
        vm.expectRevert();
        kms.addOsImageHash(keccak256("other OS"));
        vm.stopPrank();
    }

    function testKmsSelfAdmissionNeedsOsMeasurementAndDevice() public {
        UpstreamAppAuth.AppBootInfo memory b = _boot();
        (bool allowed,) = kms.isKmsAllowed(b);
        assertFalse(allowed);
        kms.addKmsAggregatedMr(b.mrAggregated);
        (allowed,) = kms.isKmsAllowed(b);
        assertFalse(allowed);
        kms.addKmsDevice(b.deviceId);
        (allowed,) = kms.isKmsAllowed(b);
        assertTrue(allowed);
        b.mrAggregated = keccak256("changed KMS");
        (allowed,) = kms.isKmsAllowed(b);
        assertFalse(allowed);
    }

    function testPermissionlessSecondOperatorAndUnknownApp() public {
        vm.prank(attacker);
        address second = factory.createApp(appCompose, bytes32(0));
        assertTrue(second != appId);
        UpstreamAppAuth.AppBootInfo memory b = _boot();
        b.appId = second;
        (bool allowed,) = kms.isAppAllowed(b);
        assertTrue(allowed);
        b.appId = address(456);
        (allowed,) = kms.isAppAllowed(b);
        assertFalse(allowed);
    }
}
