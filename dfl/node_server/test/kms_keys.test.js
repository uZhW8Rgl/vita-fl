import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { loadOrCreateKmsBundle, sealKeyBundle, unsealKeyBundle } from "../dist/kms_keys.js";
import { loadParticipantActionSigner } from "../dist/action_key.js";
import { ed25519PublicKeyFromSeed } from "../dist/sello_key.js";

const context = {appId: `0x${"11".repeat(20)}`, composeHash: `0x${"22".repeat(32)}`,
    participant: `0x${"33".repeat(20)}`, registry: `0x${"44".repeat(20)}`, chainId: "8453"};
const env = {DOCKER: "phala", PARTICIPANT_KEY_PROVIDER: "kms", ACCOUNT_ADDRESS: context.participant,
    REGISTRY_ADDRESS: context.registry, EXPECTED_CHAIN_ID: context.chainId};

test("KMS restart restores the same RSA, action and receipt identities", async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), "vita-kms-"));
    const root = crypto.randomBytes(32);
    try {
        const filename = path.join(directory, "keys.sealed.json");
        const first = await loadOrCreateKmsBundle(filename, root, context);
        const next = await loadOrCreateKmsBundle(filename, root, context, async () => {throw new Error("must not regenerate");});
        assert.deepEqual(next, first);
        const plaintext = Buffer.from("previously published model key");
        const encrypted = crypto.publicEncrypt({key: crypto.createPublicKey(first.rsa), oaepHash: "sha256"}, plaintext);
        assert.deepEqual(crypto.privateDecrypt({key: next.rsa, oaepHash: "sha256"}, encrypted), plaintext);
        const signers = await Promise.all([first, next].map(bundle => loadParticipantActionSigner({env,
            deriveKey: async () => Buffer.from(bundle.action, "hex")})));
        assert.equal(signers[0].address, signers[1].address);
        const digest = `0x${"55".repeat(32)}`;
        assert.equal(await signers[0].signDigest(digest), await signers[1].signDigest(digest));
        assert.deepEqual(ed25519PublicKeyFromSeed(Buffer.from(first.sello, "hex")), ed25519PublicKeyFromSeed(Buffer.from(next.sello, "hex")));
        const serialized = await fs.readFile(filename, "utf8");
        for (const secret of Object.values(first)) assert.equal(serialized.includes(secret), false);
        assert.equal((await fs.stat(filename)).mode & 0o777, 0o600);
        signers.forEach(signer => signer.destroy());
    } finally {root.fill(0); await fs.rm(directory, {recursive: true, force: true});}
});

test("sealed state rejects a different app, workload, participant, registry or chain", async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), "vita-kms-context-"));
    const root = crypto.randomBytes(32);
    try {
        const bundle = await loadOrCreateKmsBundle(path.join(directory, "state.json"), root, context);
        const sealed = sealKeyBundle(bundle, root, context);
        for (const field of Object.keys(context)) {
            assert.throws(() => unsealKeyBundle(sealed, root, {...context, [field]: "different"}), /context mismatch/);
        }
        assert.throws(() => unsealKeyBundle(sealed, crypto.randomBytes(32), context));
        assert.throws(() => unsealKeyBundle({...sealed, ciphertext: "AAAA"}, root, context));
    } finally {root.fill(0); await fs.rm(directory, {recursive: true, force: true});}
});

test("invalid retained state fails closed without silently creating a new identity", async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), "vita-kms-invalid-"));
    const filename = path.join(directory, "state.json");
    try {
        await fs.writeFile(filename, "corrupt retained ciphertext", {mode: 0o600});
        await assert.rejects(loadOrCreateKmsBundle(filename, crypto.randomBytes(32), context,
            async () => {throw new Error("must not regenerate");}), SyntaxError);
        assert.equal(await fs.readFile(filename, "utf8"), "corrupt retained ciphertext");
    } finally {await fs.rm(directory, {recursive: true, force: true});}
});

test("concurrent first starts converge on one authenticated persisted identity", async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), "vita-kms-race-"));
    const root = crypto.randomBytes(32);
    try {
        const filename = path.join(directory, "state.json");
        const [first, second] = await Promise.all([loadOrCreateKmsBundle(filename, root, context), loadOrCreateKmsBundle(filename, root, context)]);
        assert.deepEqual(first, second);
    } finally {root.fill(0); await fs.rm(directory, {recursive: true, force: true});}
});
