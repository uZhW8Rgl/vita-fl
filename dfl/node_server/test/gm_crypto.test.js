import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import {
    OUTPUT_BUNDLE_HASH_DOMAIN,
    buildEncryptedGlobalModelArtifacts,
    decryptEncryptedGlobalModelArtifacts,
    deriveOutputBundleHash,
} from '../dist/gm_crypto.js';

test('output bundle hash uses domain-separated length framing for every artifact', () => {
    assert.equal(
        OUTPUT_BUNDLE_HASH_DOMAIN,
        'VITA-FL:global-model-output-bundle:v2',
    );
    const artifacts = {
        encryptedBundleBytes: Buffer.from('bundle|bytes', 'utf8'),
        encryptedSignatureBytes: Buffer.from([0, 1, 2, 3, 255]),
        keyBundleBytes: Buffer.from('{"keys":["a","bc"]}', 'utf8'),
    };
    const expected = deriveOutputBundleHash(artifacts);
    assert.equal(
        expected,
        '0x02ba6ba26ef79cfb37bb33433a89d8709766657fd3f990900f9777f75c470162',
    );

    for (const [field, value] of [
        ['encryptedBundleBytes', Buffer.from('bundle|bytes!', 'utf8')],
        ['encryptedSignatureBytes', Buffer.from([0, 1, 2, 3, 254])],
        ['keyBundleBytes', Buffer.from('{"keys":["ab","c"]}', 'utf8')],
    ]) {
        assert.notEqual(
            deriveOutputBundleHash({ ...artifacts, [field]: value }),
            expected,
            `${field} was not bound by the output bundle hash`,
        );
    }
});

test('output bundle hash framing prevents concatenation-boundary ambiguity', () => {
    assert.notEqual(
        deriveOutputBundleHash({
            encryptedBundleBytes: Buffer.from('ab'),
            encryptedSignatureBytes: Buffer.from('c'),
            keyBundleBytes: Buffer.from('d'),
        }),
        deriveOutputBundleHash({
            encryptedBundleBytes: Buffer.from('a'),
            encryptedSignatureBytes: Buffer.from('bc'),
            keyBundleBytes: Buffer.from('d'),
        }),
    );
});

test('gm crypto roundtrip encrypts once and decrypts for the intended recipient', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'gm-crypto-'));
    const modelPath = path.join(tempDir, 'aggregated.bin');
    const signaturePath = path.join(tempDir, 'aggregated.bin.sig');
    const aggregationEvidencePath = path.join(tempDir, 'aggregated.hybrid-r.json');
    const encryptedBundlePath = path.join(tempDir, 'aggregated.bundle.enc');
    const encryptedSignaturePath = path.join(tempDir, 'aggregated.bundle.enc.sig');
    const keyBundlePath = path.join(tempDir, 'aggregated.bundle.keys.json');
    const outModelPath = path.join(tempDir, 'gm.bin');
    const outSignaturePath = path.join(tempDir, 'gm.bin.sig');
    const outAggregationEvidencePath = path.join(tempDir, 'gm.hybrid-r.json');

    const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const publicKeyDerHex = publicKey.export({ format: 'der', type: 'spki' }).toString('hex');

    const modelBytes = Buffer.from('encrypted-global-model-payload', 'utf8');
    const signatureBytes = Buffer.from('signed-model-bytes', 'utf8');
    const aggregationEvidenceBytes = Buffer.from(
        '{"algorithm":"hybrid-r-v1","gate_passed":true,"selected_candidate":"coordinate_median"}',
        'utf8',
    );
    await fs.writeFile(modelPath, modelBytes);
    await fs.writeFile(signaturePath, signatureBytes);
    await fs.writeFile(aggregationEvidencePath, aggregationEvidenceBytes);

    try {
        const artifacts = await buildEncryptedGlobalModelArtifacts({
            modelPath,
            signaturePath,
            aggregationEvidencePath,
            encryptedBundlePath,
            encryptedSignaturePath,
            keyBundlePath,
            recipients: [{
                address: '0x1234567890abcdef1234567890abcdef12345678',
                publicKeyDerHex: `0x${publicKeyDerHex}`,
            }],
            round: 7,
            signingPrivateKey: privateKey,
        });

        assert.equal(artifacts.recipients, 1);
        assert.equal(
            artifacts.aggregationEvidenceHash,
            `0x${crypto.createHash('sha256').update(aggregationEvidenceBytes).digest('hex')}`,
        );
        assert.equal(
            artifacts.outputBundleHash,
            deriveOutputBundleHash({
                encryptedBundleBytes: await fs.readFile(encryptedBundlePath),
                encryptedSignatureBytes: await fs.readFile(encryptedSignaturePath),
                keyBundleBytes: await fs.readFile(keyBundlePath),
            }),
        );
        const publicKeyBundle = JSON.parse(
            await fs.readFile(keyBundlePath, 'utf8'),
        );
        assert.equal(
            Buffer.from(
                publicKeyBundle.aggregation_evidence_b64,
                'base64',
            ).toString('utf8'),
            aggregationEvidenceBytes.toString('utf8'),
        );
        assert.equal(
            publicKeyBundle.aggregation_evidence_sha256,
            artifacts.aggregationEvidenceHash.slice(2),
        );
        assert.ok((await fs.readFile(encryptedBundlePath, 'utf8')).includes('"cipher":"aes-256-gcm"'));
        assert.ok((await fs.stat(encryptedSignaturePath)).size > 0);
        assert.equal(
            crypto.verify(
                'RSA-SHA256',
                await fs.readFile(encryptedBundlePath),
                publicKey,
                await fs.readFile(encryptedSignaturePath),
            ),
            true,
        );

        const result = await decryptEncryptedGlobalModelArtifacts({
            encryptedBundlePath,
            keyBundlePath,
            ownAddress: '0x1234567890abcdef1234567890abcdef12345678',
            outModelPath,
            outSignaturePath,
            outAggregationEvidencePath,
            decryptionPrivateKey: privateKey,
        });

        assert.equal(result.round, 7);
        assert.deepEqual(await fs.readFile(outModelPath), modelBytes);
        assert.deepEqual(await fs.readFile(outSignaturePath), signatureBytes);
        assert.deepEqual(
            await fs.readFile(outAggregationEvidencePath),
            aggregationEvidenceBytes,
        );
        assert.equal(result.aggregationEvidence.algorithm, 'hybrid-r-v1');
        assert.equal(result.aggregationEvidence.gate_passed, true);
    } finally {
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});

test('gm crypto rejects non-JSON Hybrid-R evidence before publication', async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'gm-crypto-evidence-'));
    const { privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    const paths = {
        modelPath: path.join(tempDir, 'aggregated.bin'),
        signaturePath: path.join(tempDir, 'aggregated.bin.sig'),
        aggregationEvidencePath: path.join(tempDir, 'aggregated.hybrid-r.json'),
        encryptedBundlePath: path.join(tempDir, 'aggregated.bundle.enc'),
        encryptedSignaturePath: path.join(tempDir, 'aggregated.bundle.enc.sig'),
        keyBundlePath: path.join(tempDir, 'aggregated.bundle.keys.json'),
    };
    await fs.writeFile(paths.modelPath, 'model');
    await fs.writeFile(paths.signaturePath, 'signature');
    await fs.writeFile(paths.aggregationEvidencePath, 'not-json');

    try {
        await assert.rejects(
            buildEncryptedGlobalModelArtifacts({
                ...paths,
                recipients: [],
                round: 1,
                signingPrivateKey: privateKey,
            }),
            /Invalid Hybrid-R aggregation evidence/,
        );
    } finally {
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
            signingPrivateKey: privateKey,
        });

        await assert.rejects(
            decryptEncryptedGlobalModelArtifacts({
                encryptedBundlePath,
                keyBundlePath,
                ownAddress: '0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                outModelPath: path.join(tempDir, 'out.bin'),
                outSignaturePath: path.join(tempDir, 'out.bin.sig'),
                decryptionPrivateKey: privateKey,
            }),
            /No wrapped GM round key found/,
        );
    } finally {
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
            signingPrivateKey: privateKey,
        });

        assert.equal(artifacts.recipients, 0);
        const keyBundle = JSON.parse(await fs.readFile(keyBundlePath, 'utf8'));
        assert.deepEqual(keyBundle.wrapped_keys_b64, {});
    } finally {
        await fs.rm(tempDir, { recursive: true, force: true });
    }
});
