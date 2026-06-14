import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import {
    buildEncryptedGlobalModelArtifacts,
    decryptEncryptedGlobalModelArtifacts,
} from '../dist/gm_crypto.js';

test('gm crypto roundtrip encrypts once and decrypts for the intended recipient', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'gm-crypto-'));
    const modelPath = path.join(tempDir, 'aggregated.bin');
    const signaturePath = path.join(tempDir, 'aggregated.bin.sig');
    const encryptedBundlePath = path.join(tempDir, 'aggregated.bundle.enc');
    const encryptedSignaturePath = path.join(tempDir, 'aggregated.bundle.enc.sig');
    const keyBundlePath = path.join(tempDir, 'aggregated.bundle.keys.json');
    const outModelPath = path.join(tempDir, 'gm.bin');
    const outSignaturePath = path.join(tempDir, 'gm.bin.sig');

    const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const publicKeyDerHex = publicKey.export({ format: 'der', type: 'spki' }).toString('hex');
    const privateKeyPem = privateKey.export({ format: 'pem', type: 'pkcs8' }).toString();

    const originalPrivateKeyEnv = process.env.RSA_PRIVATE_KEY;
    process.env.RSA_PRIVATE_KEY = privateKeyPem;

    const modelBytes = Buffer.from('encrypted-global-model-payload', 'utf8');
    const signatureBytes = Buffer.from('signed-model-bytes', 'utf8');
    await fs.writeFile(modelPath, modelBytes);
    await fs.writeFile(signaturePath, signatureBytes);

    try {
        const artifacts = await buildEncryptedGlobalModelArtifacts({
            modelPath,
            signaturePath,
            encryptedBundlePath,
            encryptedSignaturePath,
            keyBundlePath,
            recipients: [{
                address: '0x1234567890abcdef1234567890abcdef12345678',
                publicKeyDerHex: `0x${publicKeyDerHex}`,
            }],
            round: 7,
        });

        assert.equal(artifacts.recipients, 1);
        assert.ok((await fs.readFile(encryptedBundlePath, 'utf8')).includes('"cipher":"aes-256-gcm"'));
        assert.ok((await fs.stat(encryptedSignaturePath)).size > 0);

        const result = await decryptEncryptedGlobalModelArtifacts({
            encryptedBundlePath,
            keyBundlePath,
            ownAddress: '0x1234567890abcdef1234567890abcdef12345678',
            outModelPath,
            outSignaturePath,
        });

        assert.equal(result.round, 7);
        assert.deepEqual(await fs.readFile(outModelPath), modelBytes);
        assert.deepEqual(await fs.readFile(outSignaturePath), signatureBytes);
    } finally {
        if (originalPrivateKeyEnv === undefined) {
            delete process.env.RSA_PRIVATE_KEY;
        } else {
            process.env.RSA_PRIVATE_KEY = originalPrivateKeyEnv;
        }
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});

test('gm crypto rejects decryption for participants without a wrapped key', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'gm-crypto-miss-'));
    const modelPath = path.join(tempDir, 'aggregated.bin');
    const signaturePath = path.join(tempDir, 'aggregated.bin.sig');
    const encryptedBundlePath = path.join(tempDir, 'aggregated.bundle.enc');
    const encryptedSignaturePath = path.join(tempDir, 'aggregated.bundle.enc.sig');
    const keyBundlePath = path.join(tempDir, 'aggregated.bundle.keys.json');

    const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const publicKeyDerHex = publicKey.export({ format: 'der', type: 'spki' }).toString('hex');
    const privateKeyPem = privateKey.export({ format: 'pem', type: 'pkcs8' }).toString();

    const originalPrivateKeyEnv = process.env.RSA_PRIVATE_KEY;
    process.env.RSA_PRIVATE_KEY = privateKeyPem;

    await fs.writeFile(modelPath, Buffer.from('payload', 'utf8'));
    await fs.writeFile(signaturePath, Buffer.from('signature', 'utf8'));

    try {
        await buildEncryptedGlobalModelArtifacts({
            modelPath,
            signaturePath,
            encryptedBundlePath,
            encryptedSignaturePath,
            keyBundlePath,
            recipients: [{
                address: '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                publicKeyDerHex: `0x${publicKeyDerHex}`,
            }],
            round: 1,
        });

        await assert.rejects(
            decryptEncryptedGlobalModelArtifacts({
                encryptedBundlePath,
                keyBundlePath,
                ownAddress: '0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                outModelPath: path.join(tempDir, 'out.bin'),
                outSignaturePath: path.join(tempDir, 'out.bin.sig'),
            }),
            /No wrapped GM round key found/,
        );
    } finally {
        if (originalPrivateKeyEnv === undefined) {
            delete process.env.RSA_PRIVATE_KEY;
        } else {
            process.env.RSA_PRIVATE_KEY = originalPrivateKeyEnv;
        }
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});

test('gm crypto allows empty recipient sets for bootstrap rounds', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'gm-crypto-empty-'));
    const modelPath = path.join(tempDir, 'aggregated.bin');
    const signaturePath = path.join(tempDir, 'aggregated.bin.sig');
    const encryptedBundlePath = path.join(tempDir, 'aggregated.bundle.enc');
    const encryptedSignaturePath = path.join(tempDir, 'aggregated.bundle.enc.sig');
    const keyBundlePath = path.join(tempDir, 'aggregated.bundle.keys.json');

    const { privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const privateKeyPem = privateKey.export({ format: 'pem', type: 'pkcs8' }).toString();

    const originalPrivateKeyEnv = process.env.RSA_PRIVATE_KEY;
    process.env.RSA_PRIVATE_KEY = privateKeyPem;

    await fs.writeFile(modelPath, Buffer.from('bootstrap-payload', 'utf8'));
    await fs.writeFile(signaturePath, Buffer.from('bootstrap-signature', 'utf8'));

    try {
        const artifacts = await buildEncryptedGlobalModelArtifacts({
            modelPath,
            signaturePath,
            encryptedBundlePath,
            encryptedSignaturePath,
            keyBundlePath,
            recipients: [],
            round: 0,
        });

        assert.equal(artifacts.recipients, 0);
        const keyBundle = JSON.parse(await fs.readFile(keyBundlePath, 'utf8'));
        assert.deepEqual(keyBundle.wrapped_keys_b64, {});
    } finally {
        if (originalPrivateKeyEnv === undefined) {
            delete process.env.RSA_PRIVATE_KEY;
        } else {
            process.env.RSA_PRIVATE_KEY = originalPrivateKeyEnv;
        }
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});
