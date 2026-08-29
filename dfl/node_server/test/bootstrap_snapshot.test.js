import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import test from 'node:test';

import {
    buildRoundZeroBootstrapSnapshot,
    createCommittedRunRosterBinding,
    frozenRecipientsForCommittedRoster,
    parseRoundZeroBootstrapSnapshot,
    requireFrozenRecipientKeysMatchRegistry,
} from '../dist/bootstrap_snapshot.js';

const addresses = [
    '0x1234567890abcdef1234567890abcdef12345678',
    '0xabcdefabcdefabcdefabcdefabcdefabcdefabcd',
];
const registryAddress = '0x1111111111111111111111111111111111111111';
const rosterDigest = `0x${'44'.repeat(32)}`;

const binding = (overrides = {}) => createCommittedRunRosterBinding({
    chainId: overrides.chainId ?? 31337,
    registryAddress: overrides.registryAddress ?? registryAddress,
    rosterDigest: overrides.rosterDigest ?? rosterDigest,
    recipientAddresses: overrides.recipientAddresses ?? addresses,
});

const recipientEntries = () => addresses.map((address) => {
    const { publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
    return {
        address,
        publicKeyDerHex: `0x${publicKey
            .export({ format: 'der', type: 'spki' })
            .toString('hex')}`,
    };
});

test('round-0 snapshot freezes exact addresses and registered RSA DER keys', () => {
    const recipients = recipientEntries();
    const snapshot = buildRoundZeroBootstrapSnapshot(binding(), recipients);
    const restored = frozenRecipientsForCommittedRoster(snapshot, binding());

    assert.equal(snapshot.version, 3);
    assert.equal(snapshot.run_roster_digest, rosterDigest);
    assert.deepEqual(restored, recipients);
    assert.deepEqual(
        snapshot.recipients.map((entry) => entry.address),
        addresses,
    );
    assert.ok(
        snapshot.recipients.every((entry) => /^0x[0-9a-f]+$/.test(
            entry.public_key_der_hex,
        )),
    );
});

test('round-0 snapshot is bound to the immutable on-chain roster digest and order', () => {
    const snapshot = buildRoundZeroBootstrapSnapshot(binding(), recipientEntries());
    assert.throws(
        () => frozenRecipientsForCommittedRoster(
            snapshot,
            binding({ rosterDigest: `0x${'55'.repeat(32)}` }),
        ),
        /run_roster_digest/,
    );
    assert.throws(
        () => frozenRecipientsForCommittedRoster(
            snapshot,
            binding({ recipientAddresses: [...addresses].reverse() }),
        ),
        /committed (bootstrap_worker|run roster)/,
    );
});

test('round-0 snapshot recovery has no mutable MFS generation binding', () => {
    const snapshot = buildRoundZeroBootstrapSnapshot(binding(), recipientEntries());
    assert.equal('declaration_generation_sha256' in snapshot, false);
    assert.equal('admission_generation_sha256' in snapshot, false);
    assert.deepEqual(
        frozenRecipientsForCommittedRoster(snapshot, binding()),
        snapshot.recipients.map((entry) => ({
            address: entry.address,
            publicKeyDerHex: entry.public_key_der_hex,
        })),
    );
});

test('legacy snapshots fail closed instead of re-reading keys or MFS', () => {
    assert.throws(
        () => parseRoundZeroBootstrapSnapshot({
            version: 2,
            chain_id: 31337,
            registry_address: registryAddress,
            declaration_generation_sha256: '11'.repeat(32),
            admission_generation_sha256: '22'.repeat(32),
            recipients: addresses,
        }),
        /version 3 bound to the committed run roster/,
    );
});

test('snapshot recovery rejects a locally substituted recipient RSA key', () => {
    const registered = recipientEntries();
    const snapshot = buildRoundZeroBootstrapSnapshot(binding(), registered);
    const frozen = frozenRecipientsForCommittedRoster(snapshot, binding());
    const [{ publicKey: attackerKey }] = [
        crypto.generateKeyPairSync('rsa', { modulusLength: 2048 }),
    ];
    const tampered = frozen.map((entry, index) => index === 1
        ? {
            ...entry,
            publicKeyDerHex: `0x${attackerKey
                .export({ format: 'der', type: 'spki' })
                .toString('hex')}`,
        }
        : entry);

    assert.throws(
        () => requireFrozenRecipientKeysMatchRegistry(tampered, registered),
        /does not match the immutable DeviceRegistry registration/,
    );
    assert.deepEqual(
        requireFrozenRecipientKeysMatchRegistry(frozen, registered),
        frozen,
    );
});
