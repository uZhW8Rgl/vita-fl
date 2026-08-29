import assert from 'node:assert/strict';
import test from 'node:test';

import { reconcileFetchedGlobalModel } from '../dist/model_bundle.js';

const current = {
    modelCid: 'bafy-model',
    sigCid: 'bafy-outer-signature',
    keyBundleCid: 'bafy-key-bundle',
    publisher: '0x1234567890abcdef1234567890abcdef12345678',
    publisherPublicKeyDerHex: '0x30010203',
    modelRound: 1,
};

test('reconciliation preserves verified decrypted metadata with authoritative identity', () => {
    const fetched = {
        ...current,
        plaintextSignaturePresent: false,
        aggregationEvidence: null,
        aggregationEvidenceHash: null,
        untrustedExtraField: 'must-not-propagate',
    };
    const reconciled = reconcileFetchedGlobalModel(fetched, {
        ...current,
        publisher: current.publisher.toUpperCase().replace('0X', '0x'),
        publisherPublicKeyDerHex: current.publisherPublicKeyDerHex.toUpperCase().replace('0X', '0x'),
        untrustedCurrentMetadata: 'must-not-propagate',
    });

    assert.deepEqual(reconciled, {
        ...current,
        plaintextSignaturePresent: false,
        aggregationEvidence: null,
        aggregationEvidenceHash: null,
    });
    assert.equal('untrustedExtraField' in reconciled, false);
    assert.equal('untrustedCurrentMetadata' in reconciled, false);
});

test('reconciliation carries verified learned-model evidence without arbitrary fields', () => {
    const learnedIdentity = { ...current, modelRound: 2 };
    const evidence = { algorithm: 'hybrid-r-v1' };
    const reconciled = reconcileFetchedGlobalModel(
        {
            ...learnedIdentity,
            plaintextSignaturePresent: true,
            aggregationEvidence: evidence,
            aggregationEvidenceHash:
                '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        },
        learnedIdentity,
    );
    assert.equal(reconciled.aggregationEvidence, evidence);
    assert.equal(reconciled.modelRound, 2);
});

test('reconciliation rejects a finalized model-round mismatch', () => {
    assert.throws(
        () => reconcileFetchedGlobalModel(
            {
                ...current,
                modelRound: 2,
                plaintextSignaturePresent: true,
                aggregationEvidence: null,
                aggregationEvidenceHash: null,
            },
            current,
        ),
        /finalized round/,
    );
});

test('reconciliation requires verified signature-presence metadata', () => {
    assert.throws(
        () => reconcileFetchedGlobalModel(
            {
                ...current,
                aggregationEvidence: null,
                aggregationEvidenceHash: null,
            },
            current,
        ),
        /plaintext-signature metadata/,
    );
});
