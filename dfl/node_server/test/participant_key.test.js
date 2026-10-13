import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {
    loadParticipantKey,
    materializeParticipantPrivateKey,
} from "../dist/participant_key.js";

const ADDRESS = "0x1234567890abcdef1234567890abcdef12345678";

const options = {env: {DOCKER: "phala", ACCOUNT_ADDRESS: ADDRESS},
    deriveKey: async () => { throw new Error("GetKey must never be called"); }};

test("fresh participant initialization cannot decrypt data for the former RSA key", {timeout: 30_000}, async () => {
    const first = await loadParticipantKey(options);
    const next = await loadParticipantKey(options);
    assert.equal(first.source, "ephemeral-random");
    assert.equal(first.privateKey.asymmetricKeyDetails.modulusLength, 3072);
    assert.notEqual(first.publicKeyDerHex, next.publicKeyDerHex);
    const ciphertext = crypto.publicEncrypt({key: first.publicKey,
        padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256"}, Buffer.from("secret-model-key"));
    assert.equal(crypto.privateDecrypt({key: first.privateKey,
        padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256"}, ciphertext).toString(), "secret-model-key");
    assert.throws(() => crypto.privateDecrypt({key: next.privateKey,
        padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256"}, ciphertext));
    const message = Buffer.from("training-evidence");
    const signature = crypto.sign("RSA-SHA256", message, first.privateKey);
    assert.equal(crypto.verify("RSA-SHA256", message, first.publicKey, signature), true);
    assert.equal(crypto.verify("RSA-SHA256", message, next.publicKey, signature), false);
});

test("legacy persistent state and externally supplied providers cannot enter fresh Phala admission", async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), "legacy-rsa-"));
    try {
        const legacy = path.join(directory, "sealed.json");
        await fs.writeFile(legacy, "old encrypted state");
        await assert.rejects(loadParticipantKey({env: {...options.env, PARTICIPANT_KEY_STATE_PATH: legacy}}), /Legacy persistent/);
        assert.equal(await fs.readFile(legacy, "utf8"), "old encrypted state");
        for (const provider of ["file", "dstack"]) {
            await assert.rejects(loadParticipantKey({env: {...options.env, PARTICIPANT_KEY_PROVIDER: provider}}), /ephemeral key provider/);
        }
    } finally {await fs.rm(directory, {recursive:true, force:true});}
});

test("runtime PEM materialization preserves the generated public identity and restrictive permissions", async () => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), "runtime-rsa-"));
    try {
        const first = await loadParticipantKey(options);
        const privateKeyPath = path.join(directory, "private.pem");
        const publicKeyPath = path.join(directory, "public.pem");
        await materializeParticipantPrivateKey(first, {privateKeyPath, publicKeyPath});
        assert.equal((await fs.stat(privateKeyPath)).mode & 0o777, 0o600);
        const loaded = crypto.createPrivateKey(await fs.readFile(privateKeyPath));
        assert.deepEqual(Buffer.from(crypto.createPublicKey(loaded).export({format:"der", type:"spki"})), first.publicKeyDer);
    } finally {await fs.rm(directory, {recursive:true, force:true});}
});

test("local development requires the explicit file provider", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "participant-key-local-"));
    const privateKeyPath = path.join(tempDir, "private.pem");
    const { privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
    await fs.writeFile(
        privateKeyPath,
        privateKey.export({ format: "pem", type: "pkcs8" }),
        { mode: 0o600 },
    );

    try {
        await assert.rejects(
            loadParticipantKey({ env: {} }),
            /Set PARTICIPANT_KEY_PROVIDER=file/,
        );
        const loaded = await loadParticipantKey({
            env: {
                PARTICIPANT_KEY_PROVIDER: "file",
                PARTICIPANT_RSA_PRIVATE_KEY_FILE: privateKeyPath,
            },
        });
        assert.equal(loaded.source, "local-file");
        assert.deepEqual(
            loaded.publicKeyDer,
            Buffer.from(crypto.createPublicKey(privateKey).export({ format: "der", type: "spki" })),
        );
    } finally {
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});
