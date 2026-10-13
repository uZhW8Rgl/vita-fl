import assert from "node:assert/strict";
import crypto from "node:crypto";
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

test("ephemeral probe holds one identity without persisted state or GetKey and quotes the observations", async () => {
    const first = await probeKey(challenge, {env, info, quote});
    const second = await probeKey("cb".repeat(32), {env, info, quote});
    assert.equal(first.claims.key_outcome, "KEY_HELD_IN_MEMORY");
    assert.equal(first.claims.key_lifecycle, "ephemeral-workload-v1");
    assert.equal(first.claims.kms_material_obtained, false);
    assert.equal(first.claims.state_sha256, null);
    assert.equal(first.claims.rsa_public_key_sha256, second.claims.rsa_public_key_sha256);
    const expected = crypto.createHash("sha512").update("VITA-FL:key-evaluation:v1\0").update(second.claims_json).digest("hex");
    assert.equal(second.quote, expected);
    assert.equal(JSON.stringify(second).includes("PRIVATE KEY"), false);
});
