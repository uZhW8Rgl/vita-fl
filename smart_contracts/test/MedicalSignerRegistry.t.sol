// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {MedicalSignerRegistry} from "../src/core/MedicalSignerRegistry.sol";

contract MedicalSignerRegistryTest is Test {
    MedicalSignerRegistry private registry;
    address private attacker;

    bytes32 private constant DEVICE_ID = bytes32("XRAY_DEVICE_0");
    bytes32 private constant RADIOLOGIST_ID = bytes32("RADIOLOGIST_0");

    function setUp() public {
        registry = new MedicalSignerRegistry();
        attacker = makeAddr("attacker");
    }

    function testOwnerConfiguresSeparateSignerRoles() public {
        registry.configureSigner(
            DEVICE_ID,
            MedicalSignerRegistry.SignerRole.XRAY_DEVICE,
            "Synthetic X-Ray Device 0",
            hex"010203",
            hex"040506"
        );
        registry.configureSigner(
            RADIOLOGIST_ID,
            MedicalSignerRegistry.SignerRole.RADIOLOGIST,
            "Synthetic Radiologist 0",
            hex"070809",
            hex"0a0b0c"
        );

        bytes32[] memory devices =
            registry.getActiveSignerIds(MedicalSignerRegistry.SignerRole.XRAY_DEVICE);
        bytes32[] memory radiologists =
            registry.getActiveSignerIds(MedicalSignerRegistry.SignerRole.RADIOLOGIST);
        assertEq(devices.length, 1);
        assertEq(devices[0], DEVICE_ID);
        assertEq(radiologists.length, 1);
        assertEq(radiologists[0], RADIOLOGIST_ID);
        assertEq(registry.keySetVersion(), 2);

        (bool active, MedicalSignerRegistry.SignerRole role,,, bytes memory certificate, bytes32 fingerprint) =
            registry.getSigner(DEVICE_ID);
        assertTrue(active);
        assertEq(uint8(role), uint8(MedicalSignerRegistry.SignerRole.XRAY_DEVICE));
        assertEq(certificate, hex"040506");
        assertEq(fingerprint, sha256(hex"040506"));
    }

    function testOnlyOwnerCanConfigureOrChangeSigners() public {
        vm.startPrank(attacker);
        vm.expectRevert(bytes("not owner"));
        registry.configureSigner(
            DEVICE_ID,
            MedicalSignerRegistry.SignerRole.XRAY_DEVICE,
            "attacker",
            hex"01",
            hex"02"
        );
        vm.expectRevert(bytes("not owner"));
        registry.setSignerActive(DEVICE_ID, false);
        vm.stopPrank();
    }

    function testRevokedSignerDisappearsFromActiveList() public {
        registry.configureSigner(
            DEVICE_ID,
            MedicalSignerRegistry.SignerRole.XRAY_DEVICE,
            "Synthetic X-Ray Device 0",
            hex"010203",
            hex"040506"
        );
        registry.setSignerActive(DEVICE_ID, false);

        assertEq(registry.getActiveSignerIds(MedicalSignerRegistry.SignerRole.XRAY_DEVICE).length, 0);
        (bool active,,,,,) = registry.getSigner(DEVICE_ID);
        assertFalse(active);
        assertEq(registry.keySetVersion(), 2);
    }
}
