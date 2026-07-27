import test from "node:test";
import assert from "node:assert/strict";

import { deriveWorkerPolicyIdentity } from "../dist/worker_policy.js";

const IMAGE =
    "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:"
    + "4c7c8c396efc41715d27794b831c40f9e02d34bffbfd3cc2586afc6ac448d553";

const appCompose = (workerLines) => JSON.stringify({
    docker_compose_file: [
        "services:",
        "  dfl-worker:",
        `    image: ${IMAGE}`,
        "    restart: unless-stopped",
        ...workerLines,
        "",
    ].join("\n"),
    manifest_version: 2,
    runner: "docker-compose",
});

test("policy derivation matches the on-chain golden vector", () => {
    const canonical = JSON.stringify({
        docker_compose_file: [
            "services:",
            "  dfl-worker:",
            `    image: ${IMAGE}`,
            "    environment:",
            '      ROUND: "1"',
            "",
        ].join("\n"),
        manifest_version: 2,
        name: "worker-0",
        runner: "docker-compose",
    });
    assert.equal(
        deriveWorkerPolicyIdentity(canonical).workerPolicyHash,
        "0xafdf5af53b2261b7033b16222437b53335c6fa5ae3e71303e90336f272c3cf36",
    );
});

test("known variable environment values do not change the worker policy", () => {
    const first = appCompose([
        "    environment:",
        '      ACCOUNT_ADDRESS: "0x1111"',
        '      ROUND: "1"',
        '      PUBLIC_IP: "https://worker-1.example"',
        "    command:",
        "      - /bin/bash",
    ]);
    const second = appCompose([
        "    environment:",
        '      PUBLIC_IP: "https://worker-9.example"',
        '      ROUND: "9"',
        '      ACCOUNT_ADDRESS: "0x9999"',
        "    command:",
        "      - /bin/bash",
    ]);

    assert.notEqual(first, second);
    assert.equal(
        deriveWorkerPolicyIdentity(first).workerPolicyHash,
        deriveWorkerPolicyIdentity(second).workerPolicyHash,
    );
});

test("unknown environment values are hashed into the worker policy", () => {
    const safe = appCompose([
        "    environment:",
        '      ROUND: "1"',
        '      NODE_OPTIONS: "--no-warnings"',
    ]);
    const dangerous = appCompose([
        "    environment:",
        '      ROUND: "1"',
        '      NODE_OPTIONS: "--import=/tmp/evil.mjs"',
    ]);

    assert.notEqual(
        deriveWorkerPolicyIdentity(safe).workerPolicyHash,
        deriveWorkerPolicyIdentity(dangerous).workerPolicyHash,
    );
});

test("duplicate environment keys are rejected", () => {
    const duplicate = appCompose([
        "    environment:",
        '      ROUND: "1"',
        '      ROUND: "2"',
    ]);
    assert.throws(
        () => deriveWorkerPolicyIdentity(duplicate),
        /worker environment key duplicated/,
    );
});

test("unknown service-level keys are rejected", () => {
    const hostPid = appCompose([
        "    environment:",
        '      ROUND: "1"',
        "    pid: host",
    ]);
    assert.throws(
        () => deriveWorkerPolicyIdentity(hostPid),
        /unknown worker service field/,
    );
});

test("duplicate top-level services fields are rejected", () => {
    const duplicateServices = JSON.stringify({
        docker_compose_file: [
            "services:",
            "services:",
            "  dfl-worker:",
            `    image: ${IMAGE}`,
            "",
        ].join("\n"),
        manifest_version: 2,
        runner: "docker-compose",
    });
    assert.throws(
        () => deriveWorkerPolicyIdentity(duplicateServices),
        /services field duplicated/,
    );
});

test("top-level named volume cannot be redefined as a host bind", () => {
    const hostBind = appCompose([
        "    volumes:",
        "      - participant-key-state:/var/lib/vita-fl",
        "volumes:",
        "  participant-key-state:",
        "    driver_opts:",
        "      type: none",
        '      o: "bind"',
        '      device: "/tmp/attacker-controlled"',
    ]);
    assert.throws(
        () => deriveWorkerPolicyIdentity(hostBind),
        /top-level volume options not allowed/,
    );
});

test("training and inference compose roles derive distinct policies", () => {
    const training = appCompose([
        "    tmpfs:",
        "      - /run/vita-fl:size=16m,mode=0700",
        "    ports:",
        '      - "8001:8001"',
        "    environment:",
        '      ROUND: "1"',
        '      TEE_INFERENCE_ENABLED: "0"',
    ]);
    const inference = appCompose([
        "    tmpfs:",
        "      - /run/vita-fl:size=16m,mode=0700",
        "      - /tmp/tee-inference:size=256m,mode=0700",
        "    ports:",
        '      - "8001:8001"',
        '      - "8080:8080"',
        "    environment:",
        '      ROUND: "1"',
        '      TEE_INFERENCE_ENABLED: "1"',
    ]);

    assert.notEqual(
        deriveWorkerPolicyIdentity(training).workerPolicyHash,
        deriveWorkerPolicyIdentity(inference).workerPolicyHash,
    );
});
