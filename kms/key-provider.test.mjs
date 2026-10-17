import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {X509Certificate} from "node:crypto";
import {keyProviderIdentity, inspectKms} from "./inspect-kms.mjs";
import {validateProvision} from "../phala/register-kms-app.mjs";

test("root identity uses exact dstack SPKI and serialized key-provider event", async () => {
    const pem = await readFile(new URL("fixtures/test-root-ca.crt", import.meta.url), "utf8");
    const identity = keyProviderIdentity(pem);
    const spki = new X509Certificate(pem).publicKey.export({type: "spki", format: "der"}).toString("hex");
    assert.equal(identity.id, spki);
    assert.equal(Buffer.from(identity.payload, "hex").toString(), `{"name":"kms","id":"${spki}"}`);
    assert.match(identity.hash, /^0x[0-9a-f]{64}$/);
    assert.throws(() => keyProviderIdentity("invalid certificate"));
    await assert.rejects(inspectKms("http://kms.example", pem), /HTTPS origin/);
    await assert.rejects(inspectKms("https://user:pass@kms.example", pem), /HTTPS origin/);
});

test("own KMS permits Anvil only by explicit opt-in and still checks the chain", async () => {
    const {createHash} = await import("node:crypto");
    const manifest = Buffer.from("exact bytes");
    const provision = {compose_hash: createHash("sha256").update(manifest).digest("hex"), kms_info: {chain_id: 31337}};
    assert.throws(() => validateProvision(manifest, provision, 31337), /selected supported/);
    assert.equal(validateProvision(manifest, provision, 31337, {ownKms: true}), `0x${provision.compose_hash}`);
    assert.throws(() => validateProvision(manifest, provision, 8453, {ownKms: true}), /selected supported/);
});
