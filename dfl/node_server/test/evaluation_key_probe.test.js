import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { probeKey } from "../dist/evaluation_key_probe.js";

const env = { DOCKER: "phala", EVALUATION_GATES_ENABLED: "true", ACCOUNT_ADDRESS: "0x" + "12".repeat(20) };
const challenge = "ca".repeat(32);
const info = async () => ({ app_id: "fixture-app", tcb_info: { app_compose: '{"test":"manifest"}' } });
const quote = async reportData => ({ quote: reportData.toString("hex"), event_log: "fixture-not-a-hardware-quote" });

test("probe is opt-in and challenges are bounded", async () => {
    await assert.rejects(probeKey(challenge, { env: { ...env, EVALUATION_GATES_ENABLED: "false" } }), /explicit/);
    await assert.rejects(probeKey("a", { env }), /challenge/);
});

test("same wrapping material and sealed state recover key; changed root fails without replacing state", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vita-key-probe-"));
    try {
        const statePath = path.join(dir, "sealed.json");
        const options = { env, statePath, info, quote, deriveKey: async () => new Uint8Array(32).fill(1) };
        const first = await probeKey(challenge, options);
        const second = await probeKey(challenge, options);
        assert.equal(first.claims.key_outcome, "KEY_CREATED");
        assert.equal(second.claims.key_outcome, "KEY_RECOVERED");
        assert.equal(first.claims.rsa_public_key_sha256, second.claims.rsa_public_key_sha256);
        assert.equal(first.claims.state_sha256, second.claims.state_sha256);
        const bad = await probeKey(challenge, { ...options, deriveKey: async () => new Uint8Array(32).fill(2) });
        assert.equal(bad.claims.key_outcome, "UNSEAL_REJECTED");
        assert.equal(bad.claims.rsa_public_key_sha256, null);
        assert.equal(bad.claims.state_sha256, first.claims.state_sha256);
        const denied = await probeKey(challenge, { ...options, deriveKey: async () => { throw new Error("fixture denial"); } });
        assert.equal(denied.claims.key_outcome, "KMS_DERIVATION_FAILED");
        assert.equal(denied.claims.kms_material_obtained, false);
        const expected = crypto.createHash("sha512").update("VITA-FL:key-evaluation:v1\0").update(second.claims_json).digest("hex");
        assert.equal(second.quote, expected);
        assert.equal(JSON.stringify(second).includes("PRIVATE KEY"), false);
        await fs.rm(statePath);
        const third = await probeKey(challenge, options);
        assert.notEqual(third.claims.rsa_public_key_sha256, first.claims.rsa_public_key_sha256);
    } finally { await fs.rm(dir, { recursive: true, force: true }); }
});
