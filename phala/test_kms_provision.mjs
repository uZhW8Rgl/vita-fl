import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import {validateProvision} from "./register-kms-app.mjs";

const manifest = Buffer.from('{"docker_compose_file":"reviewed workload"}');
const digest = crypto.createHash("sha256").update(manifest).digest("hex");
const provision = {compose_hash: digest, kms_info: {chain_id: 8453}};
test("KMS deployment requires exact provisioned bytes on the governing chain", () => {
    assert.equal(validateProvision(manifest, provision, 8453), `0x${digest}`);
    assert.throws(() => validateProvision(Buffer.concat([manifest, Buffer.from("\n")]), provision, 8453), /Exact app_compose/);
    assert.throws(() => validateProvision(manifest, provision, 31337), /selected supported/);
    assert.throws(() => validateProvision(manifest, {...provision, kms_info: null}, 8453), /selected supported/);
});
