// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AppComposePolicy} from "../src/attestation/AppComposeImage.sol";

contract AppComposePolicyV2Test is Test {
    AppComposePolicy private policy;

    string private constant IMAGE =
        "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:"
        "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";
    bytes32 private constant IMAGE_DIGEST =
        0x4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553;
    bytes32 private constant POLICY_V2_GOLDEN =
        0xe4bda57e5c71e65b35ae363d33a338d0027033f648a9f91829d16d80f00e1864;

    function setUp() public {
        policy = new AppComposePolicy();
    }

    function testPolicyV2GoldenVector() public view {
        (bytes32 digest, bytes32 policyHash) = policy.identity(_minimal("worker-0"));
        assertEq(digest, IMAGE_DIGEST);
        assertEq(policyHash, POLICY_V2_GOLDEN);
    }

    function testVariableNameAndSafePlatformMetadataDoNotChangePolicy() public view {
        (, bytes32 minimalPolicy) = policy.identity(_minimal("worker-0"));
        (, bytes32 renamedPolicy) = policy.identity(_minimal("worker-499"));
        (, bytes32 platformPolicy) = policy.identity(
            bytes(
                string.concat(
                    '{"allowed_envs":[],"docker_compose_file":"',
                    _escapedCompose(),
                    '","features":["kms","tproxy-net"],"gateway_enabled":true,'
                    '"kms_enabled":true,"local_key_provider_enabled":false,'
                    '"manifest_version":2,"name":"one-line-deployment",'
                    '"no_instance_id":false,"pre_launch_script":"",'
                    '"public_logs":true,"public_sysinfo":true,"public_tcbinfo":true,'
                    '"runner":"docker-compose","secure_time":false,'
                    '"storage_fs":"zfs","tproxy_enabled":true}'
                )
            )
        );

        assertEq(minimalPolicy, renamedPolicy);
        assertEq(minimalPolicy, platformPolicy);
    }

    function testRuntimeRpcEndpointChangesPolicyHash() public view {
        (, bytes32 trustedPolicy) = policy.identity(_withRpc("https://trusted-runtime.example"));
        (, bytes32 proxyPolicy) = policy.identity(_withRpc("https://participant-proxy.example"));

        assertTrue(trustedPolicy != proxyPolicy);
    }

    function testRejectsWrongManifestVersionAndRunner() public {
        vm.expectRevert(bytes("manifest_version must equal 2"));
        policy.identity(
            bytes(
                string.concat(
                    '{"docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":3,"runner":"docker-compose"}'
                )
            )
        );

        vm.expectRevert(bytes("runner must be docker-compose"));
        policy.identity(
            bytes(
                string.concat(
                    '{"docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":2,"runner":"bash"}'
                )
            )
        );
    }

    function testRejectsRootCodeAndAlternativeExecutionFields() public {
        vm.expectRevert(bytes("user pre-launch script not allowed"));
        policy.identity(
            bytes(
                string.concat(
                    '{"docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":2,"pre_launch_script":"#!/bin/sh\\nid > /tmp/root",'
                    '"runner":"docker-compose"}'
                )
            )
        );

        vm.expectRevert(bytes("unknown outer app compose field"));
        policy.identity(
            bytes(
                string.concat(
                    '{"bash_script":"id","docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":2,"runner":"docker-compose"}'
                )
            )
        );

        vm.expectRevert(bytes("unknown outer app compose field"));
        policy.identity(
            bytes(
                string.concat(
                    '{"docker_compose_file":"',
                    _escapedCompose(),
                    '","init_script":"id","manifest_version":2,"runner":"docker-compose"}'
                )
            )
        );
    }

    function testRejectsOuterAdminEnvironmentAndDuplicateFields() public {
        vm.expectRevert(bytes("unsafe outer environment key"));
        policy.identity(
            bytes(
                string.concat(
                    '{"allowed_envs":["DSTACK_ROOT_PUBLIC_KEY"],"docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":2,"runner":"docker-compose"}'
                )
            )
        );

        vm.expectRevert(bytes("unsafe outer environment key"));
        policy.identity(
            bytes(
                string.concat(
                    '{"allowed_envs":["SELLO_SERVICE_SIGNING_SEED"],"docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":2,"runner":"docker-compose"}'
                )
            )
        );

        vm.expectRevert(bytes("outer app compose field duplicated"));
        policy.identity(
            bytes(
                string.concat(
                    '{"docker_compose_file":"',
                    _escapedCompose(),
                    '","manifest_version":2,"manifest_version":2,"runner":"docker-compose"}'
                )
            )
        );
    }

    function _minimal(string memory name) private pure returns (bytes memory) {
        return bytes(
            string.concat(
                '{"docker_compose_file":"',
                _escapedCompose(),
                '","manifest_version":2,"name":"',
                name,
                '","runner":"docker-compose"}'
            )
        );
    }

    function _withRpc(string memory rpcUrl) private pure returns (bytes memory) {
        return bytes(
            string.concat(
                '{"docker_compose_file":"services:\\n  dfl-worker:\\n    image: ',
                IMAGE,
                '\\n    environment:\\n      RPC_URL: \\"',
                rpcUrl,
                '\\"\\n      EXPECTED_RUNTIME_RPC_URL: \\"',
                rpcUrl,
                '\\"\\n","manifest_version":2,"runner":"docker-compose"}'
            )
        );
    }

    function _escapedCompose() private pure returns (string memory) {
        return string.concat(
            "services:\\n  dfl-worker:\\n    image: ",
            IMAGE,
            '\\n    environment:\\n      ROUND: \\"1\\"\\n'
        );
    }
}
