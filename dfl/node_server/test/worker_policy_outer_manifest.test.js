import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { deriveWorkerPolicyIdentity } from "../dist/worker_policy.js";

const IMAGE =
    "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:"
    + "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";
const POLICY_V2_GOLDEN =
    "0xe4bda57e5c71e65b35ae363d33a338d0027033f648a9f91829d16d80f00e1864";

const dockerCompose = [
    "services:",
    "  dfl-worker:",
    `    image: ${IMAGE}`,
    "    environment:",
    '      ROUND: "1"',
    "",
].join("\n");

const manifest = (overrides = {}) => JSON.stringify({
    docker_compose_file: dockerCompose,
    manifest_version: 2,
    name: "worker-0",
    runner: "docker-compose",
    ...overrides,
});
const exportedAppCodeUrl = new URL("../../../phala/app_code.txt", import.meta.url);

test("outer-manifest policy v2 matches the Solidity golden vector", () => {
    assert.equal(
        deriveWorkerPolicyIdentity(manifest()).workerPolicyHash,
        POLICY_V2_GOLDEN,
    );
});

test("worker name and safe platform metadata do not change policy", () => {
    const base = deriveWorkerPolicyIdentity(manifest()).workerPolicyHash;
    const platform = deriveWorkerPolicyIdentity(manifest({
        allowed_envs: [],
        features: ["kms", "tproxy-net"],
        gateway_enabled: true,
        kms_enabled: true,
        local_key_provider_enabled: false,
        name: "one-line-deployment",
        no_instance_id: false,
        pre_launch_script: "",
        public_logs: true,
        public_sysinfo: true,
        public_tcbinfo: true,
        secure_time: false,
        storage_fs: "zfs",
        tproxy_enabled: true,
    })).workerPolicyHash;
    assert.equal(platform, base);
});

test("real exported Phala platform pre-launch is accepted and normalized", {
    skip: !fs.existsSync(exportedAppCodeUrl)
        ? "local ignored phala/app_code.txt export is unavailable"
        : false,
}, () => {
    const appCode = JSON.parse(fs.readFileSync(exportedAppCodeUrl, "utf8"));
    const withPlatformScript = deriveWorkerPolicyIdentity(
        JSON.stringify(appCode),
    ).workerPolicyHash;
    delete appCode.pre_launch_script;
    appCode.name = "a-different-worker-name";
    const providerDefault = deriveWorkerPolicyIdentity(
        JSON.stringify(appCode),
    ).workerPolicyHash;
    assert.equal(withPlatformScript, providerDefault);
});

test("custom root code and alternate runners are rejected", () => {
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({
            pre_launch_script: "#!/bin/sh\nid > /tmp/root",
        })),
        /user pre-launch script not allowed/,
    );
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({ runner: "bash" })),
        /runner must be docker-compose/,
    );
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({ bash_script: "id" })),
        /unknown outer app compose field/,
    );
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({ init_script: "id" })),
        /unknown outer app compose field/,
    );
});

test("unsafe outer admin inputs and duplicate fields are rejected", () => {
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({
            allowed_envs: ["DSTACK_ROOT_PUBLIC_KEY"],
        })),
        /unsafe outer environment key/,
    );
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({
            allowed_envs: ["SELLO_SERVICE_SIGNING_SEED"],
        })),
        /unsafe outer environment key/,
    );
    const duplicate = `{"docker_compose_file":${JSON.stringify(dockerCompose)},`
        + '"manifest_version":2,"manifest_version":2,"runner":"docker-compose"}';
    assert.throws(
        () => deriveWorkerPolicyIdentity(duplicate),
        /outer app compose field duplicated/,
    );
});

test("manifest version 2 and docker-compose runner are mandatory", () => {
    assert.throws(
        () => deriveWorkerPolicyIdentity(manifest({ manifest_version: 3 })),
        /manifest_version must equal 2/,
    );
    const missingRunner = JSON.stringify({
        docker_compose_file: dockerCompose,
        manifest_version: 2,
    });
    assert.throws(
        () => deriveWorkerPolicyIdentity(missingRunner),
        /required outer app compose field missing/,
    );
});
