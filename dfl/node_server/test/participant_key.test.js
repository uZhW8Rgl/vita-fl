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

const dstackOptions = (statePath, keyByte = 0x42, address = ADDRESS) => ({
    env: {
        DOCKER: "phala",
        ACCOUNT_ADDRESS: address,
    },
    statePath,
    deriveKey: async () => Buffer.alloc(32, keyByte),
});

test("dstack participant RSA key is sealed, stable across restart, and fail-closed", {
    timeout: 30_000,
}, async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "participant-key-"));
    const statePath = path.join(tempDir, "state", "participant-rsa.json");
    const runtimePrivatePath = path.join(tempDir, "run", "participant-private.pem");
    const runtimePublicPath = path.join(tempDir, "run", "participant-public.pem");

    try {
        const first = await loadParticipantKey(dstackOptions(statePath));
        assert.equal(first.source, "dstack-sealed");
        assert.equal(first.privateKey.asymmetricKeyDetails.modulusLength, 3072);

        const sealed = await fs.readFile(statePath, "utf8");
        assert.doesNotMatch(sealed, /BEGIN (?:RSA )?PRIVATE KEY/);
        assert.equal((await fs.stat(path.dirname(statePath))).mode & 0o777, 0o700);
        assert.equal((await fs.stat(statePath)).mode & 0o777, 0o600);

        await materializeParticipantPrivateKey(first, {
            privateKeyPath: runtimePrivatePath,
            publicKeyPath: runtimePublicPath,
        });
        assert.equal((await fs.stat(path.dirname(runtimePrivatePath))).mode & 0o777, 0o700);
        assert.equal((await fs.stat(runtimePrivatePath)).mode & 0o777, 0o600);
        assert.equal((await fs.stat(runtimePublicPath)).mode & 0o777, 0o644);
        const materializedPrivate = crypto.createPrivateKey(await fs.readFile(runtimePrivatePath));
        const materializedPublicDer = crypto.createPublicKey(materializedPrivate)
            .export({ format: "der", type: "spki" });
        assert.deepEqual(Buffer.from(materializedPublicDer), first.publicKeyDer);

        const restarted = await loadParticipantKey(dstackOptions(statePath));
        assert.equal(restarted.publicKeyDerHex, first.publicKeyDerHex);
        const message = Buffer.from("participant-key-restart-test");
        const signature = crypto.sign("RSA-SHA256", message, restarted.privateKey);
        assert.equal(crypto.verify("RSA-SHA256", message, first.publicKey, signature), true);
        const wrapped = crypto.publicEncrypt({
            key: first.publicKey,
            padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
            oaepHash: "sha256",
        }, Buffer.from("round-key"));
        assert.equal(crypto.privateDecrypt({
            key: restarted.privateKey,
            padding: crypto.constants.RSA_PKCS1_OAEP_PADDING,
            oaepHash: "sha256",
        }, wrapped).toString(), "round-key");

        const document = JSON.parse(sealed);
        const tag = Buffer.from(document.auth_tag_b64, "base64");
        tag[0] ^= 0x01;
        document.auth_tag_b64 = tag.toString("base64");
        const tampered = `${JSON.stringify(document)}\n`;
        await fs.writeFile(statePath, tampered);
        await assert.rejects(
            loadParticipantKey(dstackOptions(statePath)),
            /Could not authenticate or load the sealed participant key/,
        );
        assert.equal(await fs.readFile(statePath, "utf8"), tampered);

        await fs.writeFile(statePath, sealed);
        await assert.rejects(
            loadParticipantKey(dstackOptions(statePath, 0x43)),
            /Could not authenticate or load the sealed participant key/,
        );
        await assert.rejects(
            loadParticipantKey(dstackOptions(
                statePath,
                0x42,
                "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )),
            /Could not authenticate or load the sealed participant key/,
        );
    } finally {
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});

test("Phala never falls back to legacy RSA environment keys", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "participant-key-fail-"));
    try {
        await assert.rejects(
            loadParticipantKey({
                env: {
                    DOCKER: "phala",
                    ACCOUNT_ADDRESS: ADDRESS,
                    RSA_PRIVATE_KEY: "legacy-private-key-must-not-be-read",
                    RSA_PUBLIC_KEY: "legacy-public-key-must-not-be-read",
                },
                statePath: path.join(tempDir, "sealed.json"),
                deriveKey: async () => {
                    throw new Error("simulated dstack outage");
                },
            }),
            /dstack participant-key derivation failed: simulated dstack outage/,
        );
        await assert.rejects(
            loadParticipantKey({
                env: {
                    DOCKER: "phala",
                    ACCOUNT_ADDRESS: ADDRESS,
                    PARTICIPANT_KEY_PROVIDER: "file",
                    PARTICIPANT_RSA_PRIVATE_KEY_FILE: "/does/not/matter",
                },
            }),
            /Phala participant keys must use the dstack key provider/,
        );
    } finally {
        await fs.rm(tempDir, { recursive: true, force: true });
    }
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
